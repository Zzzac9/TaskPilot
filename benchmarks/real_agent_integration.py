"""Production-Graph validation for real Word and Vision Agent tasks."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import time
from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import urlsplit

from pydantic import SecretStr

from benchmarks.phase13_real_llm import (
    DOCUMENT_NAME,
    build_task as build_word_task,
    evaluate_document,
)
from taskpilot.browser import BrowserConfig
from taskpilot.cli import render_progress_event
from taskpilot.config import Settings, redact_sensitive_values
from taskpilot.filesystem_mcp import create_filesystem_mcp_config
from taskpilot.models import TaskStatus, ToolCallStatus
from taskpilot.persistence import PersistenceConfig
from taskpilot.policy import ApprovalChoice, PolicyOutcome, RiskLevel
from taskpilot.service import TaskPilotService
from taskpilot.trace import TraceEvent, TraceEventType
from taskpilot.vision import VisionConfig


REPO_ROOT = Path(__file__).resolve().parents[1]
SITE_ROOT = REPO_ROOT / "tests" / "fixtures" / "browser_site"
VISION_LABEL = "BLUE CENTER"
VISION_SCENE = "center"


class IntegrationFixture:
    """Serve deterministic pages and retain a click side effect outside the Graph."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._state: dict[str, Any] = {"vision_count": 0, "vision_events": []}
        outer = self

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, directory=str(SITE_ROOT), **kwargs)

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                if self.path == "/fixture-state":
                    with outer._lock:
                        body = json.dumps(outer._state, sort_keys=True).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                super().do_GET()

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                if self.path != "/events/vision":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                with outer._lock:
                    outer._state["vision_count"] += 1
                    outer._state["vision_events"].append(payload)
                body = b'{"recorded":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "IntegrationFixture":
        self.thread.start()
        return self

    @property
    def origin(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def word_url(self) -> str:
        return self.origin + "/details.html"

    @property
    def vision_url(self) -> str:
        return self.origin + "/vision-scenes.html?scene=center&report=1"

    def state(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(_jsonable(value), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _status(value: object) -> str:
    return value.value if isinstance(value, TaskStatus) else str(value)


def _progress_sink(events: list[dict[str, Any]]):
    async def record(event: TraceEvent) -> None:
        events.append(event.model_dump(mode="json"))
        message = render_progress_event(event)
        if message is not None:
            print(message, flush=True)

    return record


def build_vision_task(url: str) -> str:
    """Natural-language goal; the production Agent chooses every tool call."""

    return (
        f"使用 Browser 工具打开 {url}。页面 Canvas 中有一个可见控件，文字为 "
        f"'{VISION_LABEL}'。请点击这个可见控件；如果系统要求人工审批，请停下来等待。"
        f"获批执行后，读取页面状态并确认它精确显示 'Clicked: {VISION_LABEL}'。"
        "只有观察到这个状态后才能完成任务。"
    )


def _tool_summary(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "tool_name": item.tool_name,
            "status": item.status.value,
            "result": _jsonable(item.result),
            "error": item.error,
        }
        for item in state.get("tool_calls", [])
    ]


async def run_word_task(
    *,
    run_dir: Path,
    source_url: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    task_dir = run_dir / "word-task"
    task_dir.mkdir(parents=True, exist_ok=False)
    filesystem_root = task_dir / "files"
    events: list[dict[str, Any]] = []
    task = build_word_task(source_url)
    state: dict[str, Any] = {}
    interrupt: dict[str, Any] | None = None
    error: str | None = None
    started = time.perf_counter()
    service = TaskPilotService(
        PersistenceConfig.from_state_dir(task_dir / "state"),
        mcp_configs=[create_filesystem_mcp_config(filesystem_root)],
        browser_allowed=True,
        browser_config=BrowserConfig(
            headless=True,
            download_dir=task_dir / "browser" / "downloads",
            artifact_dir=task_dir / "browser" / "screenshots",
        ),
    )
    try:
        async with service, asyncio.timeout(timeout_seconds):
            snapshot = await service.run_new_task(
                task,
                enable_browser=True,
                progress_handler=_progress_sink(events),
            )
            state = snapshot.state
            interrupt = snapshot.interrupt
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        error = redact_sensitive_values(f"{type(exc).__name__}: {exc}")
    oracle = evaluate_document(filesystem_root / DOCUMENT_NAME, source_url)
    status = _status(state.get("status")) if state else "failed"
    checks = {
        "production_graph_completed": status == TaskStatus.COMPLETED.value,
        "no_unhandled_interrupt": interrupt is None,
        "document_oracle_passed": bool(oracle["passed"]),
        "browser_used": any(
            item.tool_name.startswith("browser_")
            for item in state.get("tool_calls", [])
        ),
        "filesystem_mcp_used": any(
            item.tool_name == "filesystem__generate_docx"
            and item.status is ToolCallStatus.SUCCESS
            for item in state.get("tool_calls", [])
        ),
    }
    result = {
        "kind": "word",
        "task": task,
        "task_id": str(state.get("task_id") or ""),
        "task_status": status,
        "success": all(checks.values()),
        "false_completion": status == TaskStatus.COMPLETED.value
        and not bool(oracle["passed"]),
        "checks": checks,
        "oracle": oracle,
        "document_path": str(filesystem_root / DOCUMENT_NAME),
        "interrupt": interrupt,
        "tool_calls": _tool_summary(state),
        "error": error or state.get("error"),
        "duration_ms": round((time.perf_counter() - started) * 1_000, 3),
    }
    _write_json(task_dir / "result.json", result)
    _write_json(task_dir / "state.json", state)
    _write_json(task_dir / "trace.json", events)
    return result


async def _wait_for_fixture_event(fixture: IntegrationFixture) -> dict[str, Any]:
    for _ in range(40):
        state = fixture.state()
        if state["vision_count"]:
            return state
        await asyncio.sleep(0.05)
    return fixture.state()


async def _operator_decision(interrupt: dict[str, Any]) -> dict[str, str]:
    """Require an explicit terminal decision; never manufacture an approval record."""

    print("\n[approval] Production Graph is paused.", flush=True)
    print(f"[approval] type={interrupt.get('type')}", flush=True)
    print(f"[approval] tool={interrupt.get('tool_name')}", flush=True)
    print(f"[approval] risk={interrupt.get('risk_level')}", flush=True)
    print(f"[approval] reason={interrupt.get('reason')}", flush=True)
    print(
        "[approval] frozen_arguments="
        + json.dumps(
            interrupt.get("arguments_preview"), ensure_ascii=False, sort_keys=True
        ),
        flush=True,
    )
    answer = await asyncio.to_thread(
        input,
        "Type APPROVE to resume this exact local-fixture action: ",
    )
    if answer.strip() == "APPROVE":
        return {
            "decision": "approve",
            "reason": "Operator approved the displayed local fixture action",
        }
    return {
        "decision": "reject",
        "reason": "Operator did not approve the displayed action",
    }


async def run_vision_task(
    *,
    run_dir: Path,
    fixture: IntegrationFixture,
    vision_config: VisionConfig,
    timeout_seconds: int,
) -> dict[str, Any]:
    task_dir = run_dir / "vision-task"
    task_dir.mkdir(parents=True, exist_ok=False)
    events: list[dict[str, Any]] = []
    task = build_vision_task(fixture.vision_url)
    state: dict[str, Any] = {}
    interrupts: list[dict[str, Any]] = []
    error: str | None = None
    started = time.perf_counter()
    service = TaskPilotService(
        PersistenceConfig.from_state_dir(task_dir / "state"),
        mcp_configs=[create_filesystem_mcp_config(task_dir / "files")],
        browser_allowed=True,
        browser_config=BrowserConfig(
            headless=True,
            action_timeout_ms=600,
            navigation_timeout_ms=5_000,
            viewport_width=1000,
            viewport_height=650,
            download_dir=task_dir / "browser" / "downloads",
            artifact_dir=task_dir / "browser" / "screenshots",
        ),
        vision_config=vision_config,
    )
    approval_calls_before_resume: int | None = None
    try:
        async with service, asyncio.timeout(timeout_seconds):
            snapshot = await service.run_new_task(
                task,
                enable_browser=True,
                progress_handler=_progress_sink(events),
            )
            task_id = str(snapshot.state["task_id"])
            while snapshot.interrupt is not None:
                interrupt = dict(snapshot.interrupt)
                interrupts.append(interrupt)
                if approval_calls_before_resume is None:
                    approval_calls_before_resume = sum(
                        event.get("event_type")
                        in {
                            TraceEventType.VISION_FALLBACK_REQUESTED.value,
                            TraceEventType.VISION_TARGET_FOUND.value,
                            TraceEventType.VISION_TARGET_REJECTED.value,
                        }
                        for event in events
                    )
                if interrupt.get("type") != "tool_approval":
                    raise RuntimeError(
                        "Unexpected production interrupt: " + str(interrupt.get("type"))
                    )
                response = await _operator_decision(interrupt)
                snapshot = await service.resume_task(
                    task_id,
                    interrupt_type="tool_approval",
                    response=response,
                    progress_handler=_progress_sink(events),
                )
            state = snapshot.state
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        error = redact_sensitive_values(f"{type(exc).__name__}: {exc}")
    fixture_state = await _wait_for_fixture_event(fixture)
    status = _status(state.get("status")) if state else "failed"
    click_records = [
        item
        for item in state.get("tool_calls", [])
        if item.tool_name == "browser_click"
    ]
    vision_clicks = [
        item
        for item in click_records
        if item.status is ToolCallStatus.SUCCESS
        and isinstance(item.result, dict)
        and item.result.get("interaction_mode") == "vision"
    ]
    high_decisions = [
        item
        for item in state.get("policy_decisions", [])
        if item.tool_name == "browser_click"
        and item.risk_level is RiskLevel.HIGH
        and item.outcome is PolicyOutcome.REQUIRE_APPROVAL
    ]
    approvals = [
        item
        for item in state.get("approval_records", [])
        if item.tool_name == "browser_click" and item.decision is ApprovalChoice.APPROVE
    ]
    event_types = [event.get("event_type") for event in events]
    serialized_evidence = json.dumps(
        {"state": _jsonable(state), "events": events},
        ensure_ascii=False,
        default=str,
    )
    last_external_event = (
        fixture_state["vision_events"][-1] if fixture_state["vision_events"] else {}
    )
    checks = {
        "production_graph_completed": status == TaskStatus.COMPLETED.value,
        "agent_selected_browser_click": bool(click_records),
        "graph_paused_for_high_approval": bool(high_decisions) and bool(interrupts),
        "vision_not_called_before_approval": approval_calls_before_resume == 0,
        "approval_resumed_frozen_action": bool(approvals),
        "real_vision_click_succeeded": len(vision_clicks) == 1,
        "vision_trace_requested": (
            TraceEventType.VISION_FALLBACK_REQUESTED.value in event_types
        ),
        "vision_trace_found": TraceEventType.VISION_TARGET_FOUND.value in event_types,
        "external_click_exactly_once": fixture_state["vision_count"] == 1,
        "external_target_verified": (
            last_external_event.get("scene") == VISION_SCENE
            and last_external_event.get("label") == VISION_LABEL
        ),
        "no_screenshot_body_persisted": (
            "data:image" not in serialized_evidence
            and "iVBOR" not in serialized_evidence
        ),
    }
    result = {
        "kind": "vision",
        "task": task,
        "task_id": str(state.get("task_id") or ""),
        "task_status": status,
        "success": all(checks.values()),
        "false_completion": status == TaskStatus.COMPLETED.value
        and not all(
            checks[name]
            for name in (
                "real_vision_click_succeeded",
                "external_click_exactly_once",
                "external_target_verified",
            )
        ),
        "checks": checks,
        "interrupts": interrupts,
        "fixture_state": fixture_state,
        "tool_calls": _tool_summary(state),
        "policy_decisions": _jsonable(state.get("policy_decisions", [])),
        "approval_records": _jsonable(state.get("approval_records", [])),
        "error": error or state.get("error"),
        "duration_ms": round((time.perf_counter() - started) * 1_000, 3),
    }
    _write_json(task_dir / "result.json", result)
    _write_json(task_dir / "state.json", state)
    _write_json(task_dir / "trace.json", events)
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    successes = sum(item["success"] for item in results)
    false_completions = sum(item["false_completion"] for item in results)
    return {
        "tasks": len(results),
        "successes": successes,
        "success_rate": successes / len(results) if results else 0.0,
        "false_completions": false_completions,
        "false_completion_rate": (false_completions / len(results) if results else 0.0),
        "results": results,
    }


def render_report(summary: dict[str, Any]) -> str:
    lines = [
        "# Real Agent Integration Validation",
        "",
        f"- Tasks: {summary['tasks']}",
        f"- Successes: {summary['successes']}",
        f"- Success rate: {summary['success_rate']:.2%}",
        f"- False completions: {summary['false_completions']}",
        "",
    ]
    for item in summary["results"]:
        failed = [name for name, passed in item["checks"].items() if not passed]
        lines.extend(
            [
                f"## {item['kind']}",
                "",
                f"- Task: {item['task']}",
                f"- Task status: {item['task_status']}",
                f"- Success: {item['success']}",
                f"- False completion: {item['false_completion']}",
                f"- Failed checks: {failed or 'none'}",
                f"- Error: {item['error'] or 'none'}",
                "",
            ]
        )
    return "\n".join(lines)


async def async_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--task",
        choices=("all", "word", "vision"),
        default="all",
        help="Run both production tasks or one task while diagnosing a failure.",
    )
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    missing = [
        name
        for name, value in {
            "LLM_API_KEY": settings.llm_api_key,
            "LLM_MODEL": settings.llm_model,
            "VISION_API_KEY": os.getenv("VISION_API_KEY"),
        }.items()
        if not value
    ]
    if missing:
        parser.error("missing real model configuration: " + ", ".join(missing))
    vision_model = os.getenv("VISION_MODEL") or "deepseek-flash"
    if vision_model != "deepseek-flash":
        parser.error("this validation requires VISION_MODEL=deepseek-flash")
    vision_base_url = os.getenv("VISION_BASE_URL") or "https://api.deepseek.com"
    vision_config = VisionConfig(
        enabled=True,
        model=vision_model,
        api_key=SecretStr(os.environ["VISION_API_KEY"]),
        base_url=vision_base_url,
        min_confidence=0.80,
    )
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else REPO_ROOT / "artifacts" / "real-agent-integration" / timestamp
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    _write_json(
        run_dir / "environment.json",
        {
            "python": platform.python_version(),
            "text_model": settings.llm_model,
            "text_provider": urlsplit(settings.llm_base_url or "").netloc,
            "vision_model": vision_model,
            "vision_provider": urlsplit(vision_base_url).netloc,
            "api_keys_configured": True,
            "timeout_seconds": args.timeout_seconds,
            "production_graph": True,
            "scripted_tool_calls": False,
        },
    )
    results: list[dict[str, Any]] = []
    with IntegrationFixture() as fixture:
        if args.task in {"all", "word"}:
            print("[integration] running natural-language Word task", flush=True)
            results.append(
                await run_word_task(
                    run_dir=run_dir,
                    source_url=fixture.word_url,
                    timeout_seconds=args.timeout_seconds,
                )
            )
            print(
                f"[integration] word success={results[-1]['success']} "
                f"status={results[-1]['task_status']}",
                flush=True,
            )
        if args.task in {"all", "vision"}:
            print("[integration] running natural-language Vision task", flush=True)
            results.append(
                await run_vision_task(
                    run_dir=run_dir,
                    fixture=fixture,
                    vision_config=vision_config,
                    timeout_seconds=args.timeout_seconds,
                )
            )
            print(
                f"[integration] vision success={results[-1]['success']} "
                f"status={results[-1]['task_status']}",
                flush=True,
            )
    summary = summarize(results)
    _write_json(run_dir / "summary.json", summary)
    (run_dir / "report.md").write_text(render_report(summary) + "\n", encoding="utf-8")
    print(f"[integration] results={run_dir}", flush=True)
    print(
        f"[integration] success_rate={summary['success_rate']:.2%} "
        f"false_completions={summary['false_completions']}",
        flush=True,
    )
    return 0 if summary["successes"] == summary["tasks"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
