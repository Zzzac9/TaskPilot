"""Real-LLM Phase 13 Browser -> Filesystem MCP -> Word validation."""

from __future__ import annotations

import argparse
import asyncio
import json
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
from urllib.parse import urlsplit
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from taskpilot.browser import BrowserConfig
from taskpilot.cli import render_progress_event
from taskpilot.config import Settings, redact_sensitive_values
from taskpilot.filesystem_mcp import create_filesystem_mcp_config
from taskpilot.models import TaskStatus
from taskpilot.persistence import PersistenceConfig
from taskpilot.service import TaskPilotService
from taskpilot.trace import TraceEvent


REPO_ROOT = Path(__file__).resolve().parents[1]
SITE_ROOT = REPO_ROOT / "tests" / "fixtures" / "browser_site"
EXPECTED_TITLE = "Hong Kong Gym Price Research"
EXPECTED_FACTS = (
    "Harbor Gym costs HKD 420 per month.",
    "Peak Fitness costs HKD 480 per month.",
)
DOCUMENT_NAME = "gym-research.docx"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


class BrowserFixture:
    """Serve the deterministic rendered source over a real localhost socket."""

    def __init__(self) -> None:
        handler = partial(_QuietHandler, directory=str(SITE_ROOT))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "BrowserFixture":
        self.thread.start()
        return self

    @property
    def source_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/details.html"

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def build_task(source_url: str) -> str:
    """Return the fixed task used for every stability attempt."""

    return (
        f"Use the Browser tools to open {source_url} and extract every gym name "
        "and exact monthly price shown in the rendered page. Then use "
        f"filesystem__generate_docx to create {DOCUMENT_NAME}. The Word document "
        f"must have the title '{EXPECTED_TITLE}', contain one paragraph for each "
        "gym with its exact monthly price, and include the exact page URL in the "
        "sources list. Do not rely on prior knowledge and do not claim completion "
        "until the Filesystem MCP confirms document creation."
    )


def extract_docx_paragraphs(path: Path) -> list[str]:
    """Independently parse visible paragraph text from a DOCX OOXML package."""

    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with ZipFile(path) as archive:
        document = ElementTree.fromstring(archive.read("word/document.xml"))
    return [
        "".join(node.text or "" for node in item.findall(".//w:t", namespace))
        for item in document.findall(".//w:p", namespace)
    ]


def evaluate_document(path: Path, source_url: str) -> dict[str, Any]:
    """Apply fixed file/content/source checks without consulting Agent claims."""

    checks: dict[str, bool] = {
        "file_exists": path.is_file(),
        "valid_docx": False,
        "title_exact": False,
        "harbor_fact_exact": False,
        "peak_fact_exact": False,
        "source_exact": False,
    }
    paragraphs: list[str] = []
    error: str | None = None
    if checks["file_exists"]:
        try:
            paragraphs = extract_docx_paragraphs(path)
            checks["valid_docx"] = True
        except (BadZipFile, KeyError, ElementTree.ParseError, OSError) as exc:
            error = f"{type(exc).__name__}: {exc}"
    if checks["valid_docx"]:
        checks["title_exact"] = EXPECTED_TITLE in paragraphs
        checks["harbor_fact_exact"] = EXPECTED_FACTS[0] in paragraphs
        checks["peak_fact_exact"] = EXPECTED_FACTS[1] in paragraphs
        checks["source_exact"] = source_url in paragraphs
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "paragraphs": paragraphs,
        "error": error,
    }


def _status_value(value: object) -> str:
    return value.value if isinstance(value, TaskStatus) else str(value)


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


def _progress_sink(events: list[dict[str, Any]]):
    async def record(event: TraceEvent) -> None:
        events.append(event.model_dump(mode="json"))
        message = render_progress_event(event)
        if message is not None:
            print(message, flush=True)

    return record


async def run_attempt(
    attempt: int,
    *,
    run_dir: Path,
    source_url: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Run one production Graph in fully isolated state/browser/filesystem dirs."""

    attempt_dir = run_dir / f"attempt-{attempt}"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    filesystem_root = attempt_dir / "files"
    state_dir = attempt_dir / "state"
    browser_dir = attempt_dir / "browser"
    task = build_task(source_url)
    events: list[dict[str, Any]] = []
    state: dict[str, Any] = {}
    interrupt: dict[str, Any] | None = None
    error: str | None = None
    started = time.perf_counter()
    service = TaskPilotService(
        PersistenceConfig.from_state_dir(state_dir),
        mcp_configs=[create_filesystem_mcp_config(filesystem_root)],
        browser_allowed=True,
        browser_config=BrowserConfig(
            headless=True,
            download_dir=browser_dir / "downloads",
            artifact_dir=browser_dir / "screenshots",
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
    duration_ms = (time.perf_counter() - started) * 1_000
    status = _status_value(state.get("status")) if state else "failed"
    oracle = evaluate_document(filesystem_root / DOCUMENT_NAME, source_url)
    completed = status == TaskStatus.COMPLETED.value
    success = completed and bool(oracle["passed"])
    false_completion = completed and not bool(oracle["passed"])
    tool_calls = [
        {
            "tool_name": item.tool_name,
            "status": item.status.value,
            "error": item.error,
        }
        for item in state.get("tool_calls", [])
    ]
    result = {
        "attempt": attempt,
        "task": task,
        "task_id": str(state.get("task_id") or ""),
        "task_status": status,
        "interrupt": interrupt,
        "document_path": str(filesystem_root / DOCUMENT_NAME),
        "oracle": oracle,
        "success": success,
        "false_completion": false_completion,
        "error": error or state.get("error"),
        "duration_ms": round(duration_ms, 3),
        "tool_calls": tool_calls,
    }
    _write_json(attempt_dir / "result.json", result)
    _write_json(attempt_dir / "state.json", state)
    _write_json(attempt_dir / "trace.json", events)
    return result


def summarize(results: list[dict[str, Any]], *, source_url: str) -> dict[str, Any]:
    total = len(results)
    successes = sum(bool(item["success"]) for item in results)
    false_completions = sum(bool(item["false_completion"]) for item in results)
    return {
        "task": build_task(source_url),
        "attempts": total,
        "successes": successes,
        "success_rate": successes / total if total else 0.0,
        "false_completions": false_completions,
        "false_completion_rate": false_completions / total if total else 0.0,
        "results": results,
    }


def render_report(summary: dict[str, Any]) -> str:
    lines = [
        "# Phase 13 Real LLM Validation",
        "",
        f"- Attempts: {summary['attempts']}",
        f"- Successes: {summary['successes']}",
        f"- Success rate: {summary['success_rate']:.2%}",
        f"- False completions: {summary['false_completions']}",
        f"- False completion rate: {summary['false_completion_rate']:.2%}",
        "",
        "## Fixed task",
        "",
        str(summary["task"]),
        "",
        "## Attempts",
        "",
    ]
    for result in summary["results"]:
        oracle = result["oracle"]
        failed_checks = [
            name for name, passed in oracle["checks"].items() if not passed
        ]
        lines.extend(
            [
                f"### Attempt {result['attempt']}",
                "",
                f"- Status: {result['task_status']}",
                f"- Success: {result['success']}",
                f"- False completion: {result['false_completion']}",
                f"- Failed oracle checks: {failed_checks or 'none'}",
                f"- Error: {result['error'] or 'none'}",
                f"- Tool chain: {[item['tool_name'] for item in result['tool_calls']]}",
                "",
            ]
        )
    return "\n".join(lines)


async def async_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if args.attempts < 3:
        parser.error("Phase 13 validation requires at least 3 attempts")
    settings = Settings.from_env()
    missing = [
        name
        for name, value in {
            "LLM_API_KEY": settings.llm_api_key,
            "LLM_MODEL": settings.llm_model,
        }.items()
        if not value
    ]
    if missing:
        parser.error("missing real LLM configuration: " + ", ".join(missing))
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else (REPO_ROOT / "artifacts" / "phase13-real-llm" / timestamp)
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    provider = urlsplit(settings.llm_base_url or "").netloc or "default"
    _write_json(
        run_dir / "environment.json",
        {
            "python": platform.python_version(),
            "model": settings.llm_model,
            "provider": provider,
            "api_key_configured": True,
            "attempts": args.attempts,
            "timeout_seconds": args.timeout_seconds,
            "vision_enabled": False,
        },
    )
    results: list[dict[str, Any]] = []
    with BrowserFixture() as fixture:
        _write_json(
            run_dir / "task.json",
            {"source_url": fixture.source_url, "task": build_task(fixture.source_url)},
        )
        for attempt in range(1, args.attempts + 1):
            print(f"[phase13] attempt={attempt}/{args.attempts}", flush=True)
            results.append(
                await run_attempt(
                    attempt,
                    run_dir=run_dir,
                    source_url=fixture.source_url,
                    timeout_seconds=args.timeout_seconds,
                )
            )
            print(
                f"[phase13] attempt={attempt} status={results[-1]['task_status']} "
                f"oracle={results[-1]['oracle']['passed']} "
                f"false_completion={results[-1]['false_completion']}",
                flush=True,
            )
        summary = summarize(results, source_url=fixture.source_url)
    _write_json(run_dir / "summary.json", summary)
    (run_dir / "report.md").write_text(
        render_report(summary) + "\n", encoding="utf-8"
    )
    print(f"[phase13] results={run_dir}", flush=True)
    print(
        f"[phase13] success_rate={summary['success_rate']:.2%} "
        f"false_completions={summary['false_completions']}",
        flush=True,
    )
    return 0 if summary["successes"] == summary["attempts"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
