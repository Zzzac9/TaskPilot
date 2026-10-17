"""Real DeepSeek Vision fallback validation with Chromium and click oracles."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import urlencode, urlsplit

from pydantic import SecretStr

from taskpilot.browser import BrowserConfig, BrowserToolProvider
from taskpilot.config import redact_sensitive_values
from taskpilot.executor import TaskExecutor
from taskpilot.models import ExecutorAction, ExecutorActionType, ToolCallStatus
from taskpilot.policy import (
    ApprovalChoice,
    ApprovalRecord,
    DefaultRiskPolicy,
    PendingToolAction,
    PolicyOutcome,
    RiskLevel,
)
from taskpilot.tools import ToolRegistry
from taskpilot.vision import (
    MultimodalVisionTargeter,
    VisionConfig,
    VisionTarget,
    VisionTargetRequest,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SITE_ROOT = REPO_ROOT / "tests" / "fixtures" / "browser_site"
MODEL = "deepseek-flash"
SCENES = (
    ("center", "BLUE CENTER"),
    ("top_right", "ORANGE TOP RIGHT"),
    ("bottom_left", "GREEN BOTTOM LEFT"),
)


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


class BrowserFixture:
    """Serve the visual fixtures over a real localhost HTTP socket."""

    def __init__(self) -> None:
        handler = partial(_QuietHandler, directory=str(SITE_ROOT))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "BrowserFixture":
        self.thread.start()
        return self

    def url(self, scene: str) -> str:
        host, port = self.server.server_address[:2]
        query = urlencode({"scene": scene})
        return f"http://{host}:{port}/vision-scenes.html?{query}"

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class RecordingVisionTargeter:
    """Record safe evidence about the exact in-memory screenshot sent to the model."""

    def __init__(self, inner: MultimodalVisionTargeter) -> None:
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    async def locate(
        self,
        *,
        screenshot: bytes,
        request: VisionTargetRequest,
    ) -> VisionTarget:
        png_width = (
            int.from_bytes(screenshot[16:20], "big") if len(screenshot) >= 24 else 0
        )
        png_height = (
            int.from_bytes(screenshot[20:24], "big") if len(screenshot) >= 24 else 0
        )
        record: dict[str, Any] = {
            "screenshot_bytes": len(screenshot),
            "screenshot_sha256": hashlib.sha256(screenshot).hexdigest(),
            "png_signature_valid": screenshot.startswith(b"\x89PNG\r\n\x1a\n"),
            "png_width": png_width,
            "png_height": png_height,
            "request": request.model_dump(mode="json"),
            "target": None,
            "error": None,
        }
        self.calls.append(record)
        started = time.perf_counter()
        try:
            target = await self.inner.locate(screenshot=screenshot, request=request)
        except Exception as exc:
            record["error"] = redact_sensitive_values(f"{type(exc).__name__}: {exc}")
            raise
        else:
            record["target"] = target.model_dump(mode="json")
            return target
        finally:
            record["latency_ms"] = round((time.perf_counter() - started) * 1_000, 3)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def build_task(scene: str, label: str, url: str) -> str:
    return (
        f"Open the rendered Chromium page {url}. The semantic DOM locator for the "
        f"button named '{label}' must fail. After HIGH-risk approval, use the "
        f"viewport screenshot and {MODEL} to locate and click that visible canvas "
        f"control. Independently verify the click result for scene '{scene}'."
    )


async def _oracle_geometry(provider: BrowserToolProvider) -> dict[str, Any]:
    _, page = provider.session.get_page()
    value = await page.evaluate(
        """() => {
            const canvas = document.querySelector('#surface');
            const bounds = canvas.getBoundingClientRect();
            return {
                fixture: window.__visionFixture,
                canvas: {
                    left: bounds.left,
                    top: bounds.top,
                    width: bounds.width,
                    height: bounds.height
                },
                domButtonCount: document.querySelectorAll('button').length
            };
        }"""
    )
    return dict(value)


def _coordinate_oracle(
    click_result: dict[str, Any], geometry: dict[str, Any]
) -> dict[str, Any]:
    target = geometry["fixture"]["target"]
    canvas = geometry["canvas"]
    scale_x = canvas["width"] / 920
    scale_y = canvas["height"] / 500
    bounds = {
        "left": canvas["left"] + target["x"] * scale_x,
        "top": canvas["top"] + target["y"] * scale_y,
        "right": canvas["left"] + (target["x"] + target["w"]) * scale_x,
        "bottom": canvas["top"] + (target["y"] + target["h"]) * scale_y,
    }
    center = {
        "x": (bounds["left"] + bounds["right"]) / 2,
        "y": (bounds["top"] + bounds["bottom"]) / 2,
    }
    x = click_result.get("x")
    y = click_result.get("y")
    numeric = isinstance(x, (int, float)) and isinstance(y, (int, float))
    inside = bool(
        numeric
        and bounds["left"] <= x <= bounds["right"]
        and bounds["top"] <= y <= bounds["bottom"]
    )
    error = math.hypot(x - center["x"], y - center["y"]) if numeric else None
    return {
        "target_bounds": bounds,
        "target_center": center,
        "inside_target": inside,
        "center_error_px": round(error, 3) if error is not None else None,
    }


async def run_attempt(
    *,
    scene: str,
    label: str,
    attempt: int,
    url: str,
    run_dir: Path,
    vision_config: VisionConfig,
) -> dict[str, Any]:
    attempt_dir = run_dir / f"{scene}-attempt-{attempt}"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    task = build_task(scene, label, url)
    error: str | None = None
    result: dict[str, Any] = {}
    checks: dict[str, bool] = {}
    geometry: dict[str, Any] = {}
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    policy: dict[str, Any] = {}
    model_calls: list[dict[str, Any]] = []
    browser_version: str | None = None
    started = time.perf_counter()
    targeter = RecordingVisionTargeter(MultimodalVisionTargeter(vision_config))
    registry = ToolRegistry()
    browser_config = BrowserConfig(
        headless=True,
        action_timeout_ms=400,
        navigation_timeout_ms=5_000,
        viewport_width=1000,
        viewport_height=650,
        download_dir=attempt_dir / "downloads",
        artifact_dir=attempt_dir / "screenshots",
    )
    try:
        async with BrowserToolProvider(
            browser_config,
            vision_config=vision_config,
            vision_targeter=targeter,
        ) as provider:
            browser_version = provider.session.browser_version
            provider.register_tools(registry)
            await registry.invoke("browser_navigate", {"url": url})
            before = await registry.invoke(
                "browser_extract_text",
                {"locator": {"strategy": "css", "value": "#vision-status"}},
            )
            geometry = await _oracle_geometry(provider)
            locator = {"strategy": "role", "role": "button", "name": label}
            action = ExecutorAction(
                step_id=1,
                action_index=1,
                call_id=f"vision-{scene}-{attempt}",
                action_type=ExecutorActionType.TOOL,
                tool_name="browser_click",
                arguments={"locator": locator},
            )
            executor = TaskExecutor(registry, policy=DefaultRiskPolicy())
            decision = await executor.assess_action(action)
            pre_approval_calls = len(targeter.calls)
            if decision is None:
                raise RuntimeError("Risk Policy unexpectedly returned no decision")
            policy = decision.model_dump(mode="json")
            checks["dom_locator_absent"] = geometry["domButtonCount"] == 0
            checks["vision_not_called_before_approval"] = pre_approval_calls == 0
            checks["risk_is_high"] = decision.risk_level is RiskLevel.HIGH
            checks["approval_required"] = (
                decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
            )
            if not checks["risk_is_high"] or not checks["approval_required"]:
                raise RuntimeError("Vision fallback was not held behind HIGH approval")
            pending = PendingToolAction(
                call_id=action.call_id,
                step_id=action.step_id,
                tool_name=action.tool_name or "",
                arguments=action.arguments,
                policy_decision=decision,
            )
            approved = ApprovalRecord(
                call_id=action.call_id,
                step_id=action.step_id,
                tool_name=action.tool_name or "",
                decision=ApprovalChoice.APPROVE,
                reason="Local benchmark fixture approved",
            )
            execution = await executor.execute_approved_action(
                pending_action=pending,
                approval_records=[approved],
            )
            if execution.tool_call is not None:
                result = (
                    execution.tool_call.result
                    if isinstance(execution.tool_call.result, dict)
                    else {}
                )
                checks["tool_call_success"] = (
                    execution.tool_call.status is ToolCallStatus.SUCCESS
                )
                if execution.tool_call.error:
                    error = execution.tool_call.error
            else:
                checks["tool_call_success"] = False
                error = execution.error
            after = await registry.invoke(
                "browser_extract_text",
                {"locator": {"strategy": "css", "value": "#vision-status"}},
            )
            model_calls = list(targeter.calls)
            coordinate = _coordinate_oracle(result, geometry)
            screenshot = model_calls[0] if len(model_calls) == 1 else {}
            checks.update(
                vision_called_once=len(model_calls) == 1,
                real_png_input=(
                    screenshot.get("png_signature_valid") is True
                    and screenshot.get("screenshot_bytes", 0) > 1_000
                    and screenshot.get("png_width") == 1000
                    and screenshot.get("png_height") == 650
                ),
                structured_target=(
                    isinstance(screenshot.get("target"), dict)
                    and screenshot["target"].get("found") is True
                    and isinstance(screenshot["target"].get("confidence"), (int, float))
                ),
                interaction_mode_vision=result.get("interaction_mode") == "vision",
                coordinate_inside_target=coordinate["inside_target"],
                click_effect_verified=after.get("text") == f"Clicked: {label}",
            )
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if error is None:
            error = redact_sensitive_values(f"{type(exc).__name__}: {exc}")
        model_calls = list(targeter.calls)
        coordinate = _coordinate_oracle(result, geometry) if geometry else {}
    success = bool(checks) and all(checks.values())
    false_click = result.get("clicked") is True and not checks.get(
        "click_effect_verified", False
    )
    output = {
        "scene": scene,
        "attempt": attempt,
        "task": task,
        "model": MODEL,
        "browser_version": browser_version,
        "success": success,
        "false_click": false_click,
        "checks": checks,
        "policy": policy,
        "before_status": before.get("text"),
        "after_status": after.get("text"),
        "click_result": result,
        "coordinate_oracle": coordinate,
        "vision_calls": model_calls,
        "error": error,
        "duration_ms": round((time.perf_counter() - started) * 1_000, 3),
    }
    _write_json(attempt_dir / "result.json", output)
    return output


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    successes = sum(item["success"] for item in results)
    false_clicks = sum(item["false_click"] for item in results)
    return {
        "model": MODEL,
        "attempts": total,
        "successes": successes,
        "success_rate": successes / total if total else 0.0,
        "false_clicks": false_clicks,
        "false_click_rate": false_clicks / total if total else 0.0,
        "results": results,
    }


def render_report(summary: dict[str, Any]) -> str:
    lines = [
        "# Phase 11 Real Vision Validation",
        "",
        f"- Model: `{summary['model']}`",
        f"- Attempts: {summary['attempts']}",
        f"- Successes: {summary['successes']}",
        f"- Success rate: {summary['success_rate']:.2%}",
        f"- False clicks: {summary['false_clicks']}",
        "",
        "## Tasks and results",
        "",
    ]
    for item in summary["results"]:
        failed = [name for name, passed in item["checks"].items() if not passed]
        lines.extend(
            [
                f"### {item['scene']} / attempt {item['attempt']}",
                "",
                f"- Task: {item['task']}",
                f"- Success: {item['success']}",
                f"- Failed checks: {failed or 'none'}",
                f"- After status: {item['after_status']}",
                f"- Error: {item['error'] or 'none'}",
                "",
            ]
        )
    return "\n".join(lines)


async def async_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts-per-scene", type=int, default=1)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if args.attempts_per_scene < 1:
        parser.error("--attempts-per-scene must be at least 1")
    api_key = os.getenv("VISION_API_KEY")
    if not api_key:
        parser.error("VISION_API_KEY is required for the real vision benchmark")
    base_url = os.getenv("VISION_BASE_URL") or "https://api.deepseek.com"
    vision_config = VisionConfig(
        enabled=True,
        model=MODEL,
        api_key=SecretStr(api_key),
        base_url=base_url,
        min_confidence=0.80,
    )
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else REPO_ROOT / "artifacts" / "phase11-real-vision" / timestamp
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    _write_json(
        run_dir / "environment.json",
        {
            "python": platform.python_version(),
            "model": MODEL,
            "provider": urlsplit(base_url).netloc,
            "api_key_configured": True,
            "scenes": [scene for scene, _ in SCENES],
            "attempts_per_scene": args.attempts_per_scene,
        },
    )
    results: list[dict[str, Any]] = []
    with BrowserFixture() as fixture:
        for scene, label in SCENES:
            for attempt in range(1, args.attempts_per_scene + 1):
                print(f"[vision] scene={scene} attempt={attempt}", flush=True)
                result = await run_attempt(
                    scene=scene,
                    label=label,
                    attempt=attempt,
                    url=fixture.url(scene),
                    run_dir=run_dir,
                    vision_config=vision_config,
                )
                results.append(result)
                print(
                    f"[vision] scene={scene} success={result['success']} "
                    f"after={result['after_status']!r} error={result['error']!r}",
                    flush=True,
                )
    summary = summarize(results)
    _write_json(run_dir / "summary.json", summary)
    (run_dir / "report.md").write_text(render_report(summary) + "\n", encoding="utf-8")
    print(f"[vision] results={run_dir}", flush=True)
    print(
        f"[vision] success_rate={summary['success_rate']:.2%} "
        f"false_clicks={summary['false_clicks']}",
        flush=True,
    )
    return 0 if summary["successes"] == summary["attempts"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
