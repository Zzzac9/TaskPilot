"""真实生产组件 E2E runner；Fake 不能进入 Primary Task Success Rate。"""

import argparse
import asyncio
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.error import URLError
from urllib.request import Request, urlopen

from taskpilot.browser import BrowserConfig
from taskpilot.mcp.config import MCPServerConfig, MCPTransport
from taskpilot.models import TaskStatus
from taskpilot.persistence import PersistenceConfig
from taskpilot.service import TaskPilotService
from taskpilot.trace import SQLiteTraceRecorder, TraceEventType
from taskpilot.vision import VisionConfig

from benchmarks.taskpilot_bench.environment import (
    capture_environment,
    load_benchmark_dotenv,
    redact_secrets,
)
from benchmarks.taskpilot_bench.loader import load_manifest, render_task
from benchmarks.taskpilot_bench.metrics import aggregate_runs
from benchmarks.taskpilot_bench.models import (
    BenchmarkRunRecord,
    BenchmarkTask,
    OracleResult,
)
from benchmarks.taskpilot_bench.oracle import evaluate_oracle
from benchmarks.taskpilot_bench.reporting import (
    render_report,
    write_json_exclusive,
    write_runs_jsonl,
    write_summary_csv,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = REPO_ROOT / "benchmarks" / "manifest.json"
DEFAULT_RESULTS = REPO_ROOT / "benchmarks" / "results"


def _status_value(value: Any) -> str:
    return value.value if isinstance(value, TaskStatus) else str(value)


def _free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _post_json(url: str) -> None:
    request = Request(
        url, method="POST", data=b"{}", headers={"Content-Type": "application/json"}
    )
    with urlopen(request, timeout=5):  # noqa: S310 - hardcoded localhost fixture
        return


class FixtureServer:
    """每次正式 run 使用一个真实 localhost subprocess。"""

    def __init__(self) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen[str] | None = None

    def __enter__(self) -> "FixtureServer":
        command = [
            sys.executable,
            str(REPO_ROOT / "benchmarks" / "fixtures" / "fixture_server.py"),
            "--port",
            str(self.port),
        ]
        self.process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                output = self.process.stdout.read() if self.process.stdout else ""
                raise RuntimeError(f"fixture server exited early: {output}")
            try:
                with urlopen(f"{self.base_url}/fixture-state", timeout=0.5):  # noqa: S310
                    return self
            except (URLError, TimeoutError):
                time.sleep(0.05)
        self.__exit__(None, None, None)
        raise TimeoutError("localhost fixture server did not become ready")

    def reset(self) -> None:
        _post_json(f"{self.base_url}/reset")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None


def require_real_llm_config() -> dict[str, str]:
    """Primary smoke 前硬失败；只返回 provider host/model，不返回 key。"""

    missing = [name for name in ("LLM_API_KEY", "LLM_MODEL") if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Real LLM smoke gate blocked; missing environment variables: "
            + ", ".join(missing)
        )
    base_url = os.getenv("LLM_BASE_URL", "default OpenAI endpoint")
    host = base_url
    if "://" in base_url:
        from urllib.parse import urlsplit

        host = urlsplit(base_url).netloc
    model = str(os.environ["LLM_MODEL"])
    if any(character in model for character in "\r\n"):
        raise RuntimeError("LLM_MODEL contains forbidden control characters")
    return {"base_host": host, "model": model}


def _mcp_config() -> MCPServerConfig:
    return MCPServerConfig(
        server_id="benchmark",
        transport=MCPTransport.STDIO,
        command=sys.executable,
        args=[str(REPO_ROOT / "benchmarks" / "fixtures" / "mcp_server.py")],
        trust_tool_annotations=True,
    )


def _scripted_response(
    task: BenchmarkTask, interrupt: dict[str, Any]
) -> dict[str, Any]:
    """脚本只扮演 Human；生产 Graph 仍负责发起 interrupt。"""

    interrupt_type = str(interrupt.get("type"))
    if interrupt_type == "tool_approval":
        if "reject" in task.tags:
            return {"decision": "reject", "reason": "benchmark scripted human reject"}
        return {"decision": "approve"}
    if interrupt_type == "ambiguous_tool_recovery":
        if "abort" in task.tags:
            return {"choice": "abort", "reason": "benchmark scripted human abort"}
        return {"choice": "retry", "reason": "benchmark scripted one-shot retry"}
    raise RuntimeError(f"unsupported interrupt type: {interrupt_type}")


def _failure_category(
    error: BaseException | None, status: str, oracle: OracleResult | None
) -> str | None:
    if isinstance(error, TimeoutError):
        return "timeout"
    if error is not None:
        text = str(error).casefold()
        for name in ("planner", "browser", "mcp", "verification", "context"):
            if name in text:
                return f"{name}_error"
        return "environment_error"
    if status == "completed" and oracle is not None and not oracle.passed:
        return "verification_error"
    if status != "completed":
        return "unknown"
    return None


async def _trace_counts(
    config: PersistenceConfig, task_id: str
) -> tuple[dict[TraceEventType, int], int]:
    async with SQLiteTraceRecorder(config.trace_db_path) as trace:
        events = await trace.list_events(task_id)
        counts = await trace.count_by_type(task_id)
    return counts, len(events)


def _discover_trace_task_id(config: PersistenceConfig) -> str | None:
    """异常中断时，从该 benchmark task 的隔离 Trace DB 恢复 task_id。"""

    path = config.trace_db_path.expanduser().resolve()
    if not path.is_file():
        return None
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        rows = connection.execute(
            "SELECT DISTINCT task_id FROM trace_events LIMIT 2"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        if connection is not None:
            connection.close()
    return str(rows[0][0]) if len(rows) == 1 else None


async def run_one_task(
    run_id: str,
    task: BenchmarkTask,
    *,
    attempt: int,
    run_dir: Path,
    max_replans: int,
) -> BenchmarkRunRecord:
    """启动后的任何异常/timeout 都生成 failed raw record。"""

    state_dir = run_dir / "state" / f"{task.id}-attempt-{attempt}"
    persistence = PersistenceConfig.from_state_dir(state_dir)
    started = time.perf_counter()
    state: dict[str, Any] = {}
    task_status = "failed"
    oracle: OracleResult | None = None
    error: BaseException | None = None
    counts: dict[TraceEventType, int] = {}
    event_count = 0
    try:
        service = TaskPilotService(
            persistence,
            mcp_configs=([_mcp_config()] if task.requires_mcp else []),
            browser_allowed=task.requires_browser,
            browser_config=BrowserConfig(headless=True),
            vision_config=VisionConfig.from_env(),
            max_replans=max_replans,
        )
        async with asyncio.timeout(task.timeout_seconds), service:
            snapshot = await service.run_new_task(
                task.prompt, enable_browser=task.requires_browser
            )
            while snapshot.interrupt is not None:
                response = _scripted_response(task, snapshot.interrupt)
                snapshot = await service.resume_task(
                    str(snapshot.state["task_id"]),
                    interrupt_type=str(snapshot.interrupt["type"]),
                    response=response,
                )
            state = snapshot.state
            task_status = _status_value(state.get("status"))
            oracle = await asyncio.to_thread(evaluate_oracle, task, state)
    except TimeoutError as exc:
        error = exc
    except BaseException as exc:  # raw denominator 必须捕获 provider/runtime failure
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        error = exc
    finally:
        task_id = str(state.get("task_id") or "") or (
            _discover_trace_task_id(persistence) or ""
        )
        if task_id:
            try:
                counts, event_count = await _trace_counts(persistence, task_id)
            except Exception:
                counts, event_count = {}, 0
    latency = (time.perf_counter() - started) * 1000
    oracle_passed = oracle.passed if oracle is not None else False
    success = task_status == TaskStatus.COMPLETED.value and oracle_passed
    false_completion = task_status == TaskStatus.COMPLETED.value and not oracle_passed
    plan_revision = int(state.get("plan_revision", 0))
    safe_error = str(redact_secrets(str(error))) if error is not None else None
    return BenchmarkRunRecord(
        run_id=run_id,
        task_id=task.id,
        category=task.category,
        attempt=attempt,
        task_status=task_status,
        oracle_passed=oracle_passed,
        success=success,
        error=safe_error,
        failure_category=_failure_category(error, task_status, oracle),
        wall_latency_ms=latency,
        tool_call_count=counts.get(TraceEventType.TOOL_STARTED, 0),
        tool_failure_count=counts.get(TraceEventType.TOOL_FAILED, 0),
        llm_action_count=counts.get(TraceEventType.LLM_ACTION_DECIDED, 0),
        replan_count=counts.get(TraceEventType.PLAN_REPLANNED, 0),
        step_rejection_count=counts.get(TraceEventType.STEP_REJECTED, 0),
        approval_request_count=counts.get(TraceEventType.APPROVAL_REQUESTED, 0),
        recovery_required_count=counts.get(TraceEventType.RECOVERY_REQUIRED, 0),
        plan_revision=plan_revision,
        trace_event_count=event_count,
        false_completion=false_completion,
        token_usage=None,
        oracle=oracle,
    )


def _short_sha() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def create_run_id(label: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{_short_sha()}-{label}"


async def run_task_set(
    tasks: Sequence[BenchmarkTask],
    *,
    label: str,
    max_replans: int,
    attempt: int = 1,
    results_root: Path = DEFAULT_RESULTS,
) -> Path:
    load_benchmark_dotenv(REPO_ROOT / ".env")
    llm = require_real_llm_config()
    print(f"model={llm['model']}", flush=True)
    print(f"base_host={llm['base_host']}", flush=True)
    print("temperature=0", flush=True)
    run_id = create_run_id(label)
    run_dir = results_root / run_id
    environment, freeze, pip_check = capture_environment(REPO_ROOT)
    if (
        environment["pip_check_exit_code"] != 0
        or pip_check.strip() != "No broken requirements found."
    ):
        raise RuntimeError(f"clean environment gate failed: {pip_check}")
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json_exclusive(run_dir / "environment.json", environment)
    (run_dir / "pip-freeze.txt").write_text(freeze + "\n", encoding="utf-8")
    (run_dir / "pip-check.txt").write_text(pip_check + "\n", encoding="utf-8")
    write_json_exclusive(
        run_dir / "config.json",
        {
            "run_id": run_id,
            "label": label,
            "max_replans": max_replans,
            "temperature": 0,
            "seed": "unavailable",
            "scripted_human_only": True,
        },
    )
    with FixtureServer() as fixture:
        rendered = [render_task(task, {"base_url": fixture.base_url}) for task in tasks]
        write_json_exclusive(
            run_dir / "tasks.json",
            [task.model_dump(mode="json") for task in rendered],
        )
        records: list[BenchmarkRunRecord] = []
        for index, task in enumerate(rendered, start=1):
            fixture.reset()
            print(f"[{index}/{len(rendered)}] {task.id}", flush=True)
            record = await run_one_task(
                run_id,
                task,
                attempt=attempt,
                run_dir=run_dir,
                max_replans=max_replans,
            )
            records.append(record)
            print(
                f"  status={record.task_status} oracle={record.oracle_passed} success={record.success}",
                flush=True,
            )
    write_runs_jsonl(run_dir / "runs.jsonl", records)
    summary = aggregate_runs(run_id, records, declared_tasks=len(tasks))
    write_json_exclusive(run_dir / "summary.json", summary.model_dump(mode="json"))
    write_summary_csv(run_dir / "summary.csv", summary)
    report = render_report(
        summary,
        environment=environment,
        failures=[record for record in records if not record.success],
        reliability=None,
        replan_evaluation=None,
        stability=None,
        raw_path=str(run_dir / "runs.jsonl"),
    )
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    return run_dir


def select_smoke(tasks: Sequence[BenchmarkTask]) -> list[BenchmarkTask]:
    ids = {"mcp_record_02", "browser_table_03", "aggregate_cross_source_01"}
    selected = [task for task in tasks if task.id in ids]
    if len(selected) != 3:
        raise ValueError("3-task smoke selection incomplete")
    return selected


def select_stability(tasks: Sequence[BenchmarkTask]) -> list[BenchmarkTask]:
    """固定 10 题，不根据上一轮成败改变 subset。"""

    selected = [task for task in tasks if "stability" in task.tags]
    if len(selected) != 10:
        raise ValueError("stability subset 必须固定为 10 个 task")
    return selected


async def async_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TaskPilot Phase 12 benchmark runner")
    parser.add_argument(
        "mode",
        choices=(
            "smoke",
            "primary",
            "no-replan",
            "stability-2",
            "stability-3",
        ),
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS)
    args = parser.parse_args(argv)
    tasks = load_manifest(args.manifest)
    if args.mode == "smoke":
        tasks = select_smoke(tasks)
        max_replans = 2
        attempt = 1
    elif args.mode == "no-replan":
        tasks = [task for task in tasks if task.category.value == "replanning"]
        max_replans = 0
        attempt = 1
    elif args.mode.startswith("stability-"):
        tasks = select_stability(tasks)
        max_replans = 2
        attempt = int(args.mode.rsplit("-", 1)[1])
    else:
        max_replans = 2
        attempt = 1
    run_dir = await run_task_set(
        tasks,
        label=args.mode,
        max_replans=max_replans,
        attempt=attempt,
        results_root=args.results_root,
    )
    print(f"run_dir={run_dir}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(async_main(argv))
    except Exception as exc:
        print(f"BENCHMARK_STOP={redact_secrets(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
