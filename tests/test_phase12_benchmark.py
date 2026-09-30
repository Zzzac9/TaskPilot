"""Phase 12 benchmark harness 的纯离线语义测试。"""

import json
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

from benchmarks.taskpilot_bench.environment import (
    capture_environment,
    load_benchmark_dotenv,
    redact_secrets,
)
from benchmarks.taskpilot_bench.finalize import classify_failure
from benchmarks.taskpilot_bench.loader import load_manifest, render_task
from benchmarks.taskpilot_bench.metrics import (
    aggregate_runs,
    percentile_nearest_rank,
    replan_ablation,
    stability_summary,
    wilson_interval,
)
from benchmarks.taskpilot_bench.models import (
    BenchmarkCategory,
    BenchmarkRunRecord,
    BenchmarkTask,
    OracleResult,
    OracleType,
    ReliabilityCaseRecord,
    RunDisposition,
)
from benchmarks.taskpilot_bench.oracle import evaluate_oracle
from benchmarks.taskpilot_bench.reliability import aggregate_reliability
from benchmarks.taskpilot_bench.runner import (
    FixtureServer,
    _discover_trace_task_id,
    select_stability,
)
from benchmarks.taskpilot_bench.reporting import read_runs_jsonl, write_runs_jsonl
from taskpilot.mcp import MCPServerConfig, MCPToolProvider
from taskpilot.config import Settings
from taskpilot.llm import create_chat_model
from taskpilot.persistence import PersistenceConfig
from taskpilot.tools import ToolRegistry
from taskpilot.trace import SQLiteTraceRecorder, TraceEventType, make_trace_event


def task(**updates: object) -> BenchmarkTask:
    values: dict[str, object] = {
        "id": "task-1",
        "category": BenchmarkCategory.MCP,
        "prompt": "do work",
        "oracle_type": OracleType.STATE_CONTAINS,
        "oracle_config": {
            "checks": [
                {"name": "value", "path": "task_results", "contains": {"value": 7}}
            ]
        },
        "timeout_seconds": 30,
    }
    values.update(updates)
    return BenchmarkTask.model_validate(values)


def run_record(**updates: object) -> BenchmarkRunRecord:
    values: dict[str, object] = {
        "run_id": "run-1",
        "task_id": "task-1",
        "category": BenchmarkCategory.MCP,
        "attempt": 1,
        "task_status": "completed",
        "oracle_passed": True,
        "success": True,
        "error": None,
        "wall_latency_ms": 100,
        "tool_call_count": 2,
        "tool_failure_count": 0,
        "llm_action_count": 3,
        "replan_count": 0,
        "step_rejection_count": 0,
        "approval_request_count": 0,
        "recovery_required_count": 0,
        "plan_revision": 0,
        "trace_event_count": 10,
        "false_completion": False,
        "token_usage": None,
        "oracle": OracleResult(passed=True, reason="independent pass"),
    }
    values.update(updates)
    return BenchmarkRunRecord.model_validate(values)


def test_deepseek_chat_model_disables_thinking_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_chat_openai(**options: object) -> object:
        captured.update(options)
        return object()

    monkeypatch.setattr("taskpilot.llm.ChatOpenAI", fake_chat_openai)
    create_chat_model(
        Settings(
            llm_api_key="test-secret",
            llm_model="deepseek-v4-flash",
            llm_base_url="https://api.deepseek.com/v1",
        )
    )

    assert captured["temperature"] == 0
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


def test_non_deepseek_chat_model_does_not_receive_provider_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_chat_openai(**options: object) -> object:
        captured.update(options)
        return object()

    monkeypatch.setattr("taskpilot.llm.ChatOpenAI", fake_chat_openai)
    create_chat_model(
        Settings(
            llm_api_key="test-secret",
            llm_model="compatible-model",
            llm_base_url="https://llm.example.test/v1",
        )
    )

    assert "extra_body" not in captured


@pytest.mark.asyncio
async def test_trace_task_id_can_be_recovered_after_graph_exception(
    tmp_path: Path,
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    async with SQLiteTraceRecorder(config.trace_db_path) as recorder:
        await recorder.record(
            make_trace_event(
                task_id="task-from-trace",
                event_type=TraceEventType.TASK_STARTED,
            )
        )

    assert _discover_trace_task_id(config) == "task-from-trace"


def test_primary_manifest_is_fixed_30_with_declared_distribution() -> None:
    tasks = load_manifest(Path("benchmarks/manifest.json"))
    assert len(tasks) == 30
    assert len({item.id for item in tasks}) == 30
    assert not any(item.requires_vision for item in tasks)
    assert len(select_stability(tasks)) == 10
    rendered = [
        render_task(item, {"base_url": "http://127.0.0.1:9999"}) for item in tasks
    ]
    assert all("{base_url}" not in item.prompt for item in rendered)
    assert all("{base_url}" not in json.dumps(item.oracle_config) for item in rendered)


def test_success_requires_completed_and_external_oracle() -> None:
    with pytest.raises(ValidationError, match="success 必须"):
        run_record(task_status="running", success=True)
    with pytest.raises(ValidationError, match="success 必须"):
        run_record(oracle_passed=False, success=True)


def test_false_completion_definition() -> None:
    record = run_record(
        oracle_passed=False,
        success=False,
        false_completion=True,
        oracle=OracleResult(passed=False, reason="ground truth mismatch"),
    )
    assert record.false_completion is True
    with pytest.raises(ValidationError, match="false_completion"):
        run_record(oracle_passed=False, success=False, false_completion=False)


def test_executed_failures_remain_in_denominator() -> None:
    records = [
        run_record(task_id="ok"),
        run_record(
            task_id="failed",
            task_status="failed",
            oracle_passed=False,
            success=False,
            error="model error",
            false_completion=False,
            oracle=OracleResult(passed=False, reason="not satisfied"),
        ),
    ]
    summary = aggregate_runs("run-1", records, declared_tasks=2)
    assert summary.executed_tasks == 2
    assert summary.succeeded_tasks == 1
    assert summary.failed_tasks == 1
    assert summary.task_success_rate.denominator == 2


def test_not_run_is_separate_from_primary_denominator() -> None:
    not_run = run_record(
        task_id="vision",
        disposition=RunDisposition.NOT_RUN,
        task_status=None,
        oracle_passed=None,
        success=False,
        false_completion=False,
        error="real vision capability unavailable",
        oracle=None,
    )
    summary = aggregate_runs("run-1", [run_record(), not_run], declared_tasks=2)
    assert summary.not_run_tasks == 1
    assert summary.executed_tasks == 1
    assert summary.task_success_rate.denominator == 1


def test_runtime_failure_cannot_be_relabelled_not_run_after_execution() -> None:
    failed = run_record(
        task_status="failed",
        oracle_passed=False,
        success=False,
        false_completion=False,
        error="timeout",
        oracle=OracleResult(passed=False, reason="not observed"),
    )
    assert failed.disposition is RunDisposition.EXECUTED
    assert aggregate_runs("run-1", [failed], declared_tasks=1).failed_tasks == 1


def test_wilson_interval_is_deterministic() -> None:
    interval = wilson_interval(24, 30)
    assert interval is not None
    assert interval.lower == pytest.approx(0.6269, abs=0.0001)
    assert interval.upper == pytest.approx(0.9049, abs=0.0001)
    assert wilson_interval(0, 0) is None


def test_category_aggregation_does_not_hide_hard_category() -> None:
    records = [
        run_record(task_id="mcp", category=BenchmarkCategory.MCP),
        run_record(
            task_id="replan",
            category=BenchmarkCategory.REPLANNING,
            task_status="failed",
            oracle_passed=False,
            success=False,
            false_completion=False,
            oracle=OracleResult(passed=False, reason="failed"),
        ),
    ]
    summary = aggregate_runs("run-1", records, declared_tasks=2)
    by_category = {item.category: item.success for item in summary.category_results}
    assert by_category[BenchmarkCategory.MCP].numerator == 1
    assert by_category[BenchmarkCategory.REPLANNING].numerator == 0


def test_replan_recovery_rate_uses_only_accepted_replans() -> None:
    records = [
        run_record(task_id="recovered", replan_count=1),
        run_record(
            task_id="not-recovered",
            replan_count=1,
            task_status="failed",
            oracle_passed=False,
            success=False,
            false_completion=False,
            oracle=OracleResult(passed=False, reason="failed"),
        ),
        run_record(task_id="no-replan"),
    ]
    metric = aggregate_runs("run-1", records, declared_tasks=3).replan_recovery_rate
    assert (metric.numerator, metric.denominator, metric.percentage) == (1, 2, 50.0)


def test_mean_median_and_nearest_rank_p95() -> None:
    records = [
        run_record(task_id=str(index), wall_latency_ms=float(index))
        for index in range(1, 21)
    ]
    summary = aggregate_runs("run-1", records, declared_tasks=20)
    assert summary.mean_wall_latency_ms == 10.5
    assert summary.median_wall_latency_ms == 10.5
    assert summary.p95_wall_latency_ms == 19.0
    assert percentile_nearest_rank([], 0.95) is None


def test_jsonl_roundtrip_preserves_null_token_usage(tmp_path: Path) -> None:
    path = tmp_path / "runs.jsonl"
    records = [run_record(task_id="a"), run_record(task_id="b", token_usage=123)]
    write_runs_jsonl(path, records)
    restored = read_runs_jsonl(path)
    assert restored == records
    assert restored[0].token_usage is None


def test_secret_redaction_is_recursive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "live-secret")
    safe = redact_secrets(
        {"api_key": "live-secret", "nested": ["prefix-live-secret", {"password": "p"}]}
    )
    assert "live-secret" not in json.dumps(safe)
    assert safe["api_key"] == "[REDACTED]"


def test_environment_metadata_contains_no_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "do-not-record")
    monkeypatch.setenv("LLM_MODEL", "benchmark-model")
    metadata, _, _ = capture_environment(Path.cwd())
    rendered = json.dumps(metadata)
    assert "do-not-record" not in rendered
    assert "LLM_API_KEY" not in rendered
    assert metadata["model_name"] == "benchmark-model"


def test_external_oracle_ignores_internal_verification_result() -> None:
    state = {
        "status": "completed",
        "verification": {"completed": True, "reason": "agent says yes"},
        "task_results": {"value": 3},
    }
    result = evaluate_oracle(task(), state)
    assert result.passed is False


def test_stale_browser_execution_is_counted_as_safety_failure() -> None:
    case = ReliabilityCaseRecord(
        case_id="stale",
        mechanism="service recreation",
        passed=False,
        duplicate_unsafe_side_effects=0,
        terminal_journal_dedup_failures=0,
        recovery_interrupts_required=0,
        recovery_cases_resolved=0,
        browser_stale_runtime_executions=1,
        unauthorized_side_effects_before_approval=0,
        command=["pytest", "case"],
        exit_code=1,
        output="failed",
    )
    assert aggregate_reliability([case]).browser_stale_runtime_executions == 1


def test_duplicate_side_effect_metric_is_raw_sum() -> None:
    cases = [
        ReliabilityCaseRecord(
            case_id=f"case-{index}",
            mechanism="fault injection",
            passed=index == 0,
            duplicate_unsafe_side_effects=index,
            terminal_journal_dedup_failures=0,
            recovery_interrupts_required=1,
            recovery_cases_resolved=int(index == 0),
            browser_stale_runtime_executions=0,
            unauthorized_side_effects_before_approval=0,
            command=["pytest"],
            exit_code=index,
            output="raw",
        )
        for index in (0, 1)
    ]
    summary = aggregate_reliability(cases)
    assert summary.duplicate_unsafe_side_effects == 1
    assert summary.cases_passed == 1


def test_dotenv_loader_only_loads_allowlisted_values_without_overriding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "LLM_API_KEY=file-secret\nLLM_MODEL=deepseek-chat\nUNRELATED=value\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_API_KEY", "shell-secret")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("UNRELATED", raising=False)
    load_benchmark_dotenv(dotenv)
    assert __import__("os").environ["LLM_API_KEY"] == "shell-secret"
    assert __import__("os").environ["LLM_MODEL"] == "deepseek-chat"
    assert "UNRELATED" not in __import__("os").environ


def test_replan_ablation_requires_identical_task_grouping() -> None:
    full = [run_record(task_id="r1"), run_record(task_id="r2")]
    no_replan = [
        run_record(
            task_id="r1",
            task_status="failed",
            oracle_passed=False,
            success=False,
            false_completion=False,
            oracle=OracleResult(passed=False, reason="dead end"),
        ),
        run_record(task_id="r2"),
    ]
    result = replan_ablation(full, no_replan)
    assert result["delta_percentage_points"] == 50.0
    with pytest.raises(ValueError, match="task IDs"):
        replan_ablation(full, no_replan[:1])


def test_stability_summary_requires_three_numbered_attempts() -> None:
    records = [
        run_record(task_id="stable", attempt=attempt)
        for attempt in (1, 2, 3)
    ] + [
        run_record(
            task_id="variable",
            attempt=attempt,
            task_status=("completed" if attempt == 1 else "failed"),
            oracle_passed=(True if attempt == 1 else False),
            success=(attempt == 1),
            false_completion=False,
            oracle=OracleResult(
                passed=(attempt == 1),
                reason="attempt result",
            ),
        )
        for attempt in (1, 2, 3)
    ]

    summary = stability_summary(records)

    assert summary["per_task_success_count"] == {"stable": 3, "variable": 1}
    assert summary["all_three_success_count"] == 1
    assert summary["instability_count"] == 1
    assert summary["subset_aggregate_success_variance"] == pytest.approx(
        0.05555555555555555
    )


def test_failure_analysis_keeps_false_completion_and_executor_contract_distinct() -> None:
    false_completion = run_record(
        oracle_passed=False,
        success=False,
        false_completion=True,
        oracle=OracleResult(passed=False, reason="ground truth mismatch"),
    )
    wrong_tool_selection = run_record(
        task_status="failed",
        oracle_passed=False,
        success=False,
        false_completion=False,
        oracle=OracleResult(
            passed=False,
            reason="not completed",
            observed_output={"error": "Executor 每轮只能返回一个 Tool Call; 实际数量: 2"},
        ),
    )

    assert classify_failure(false_completion) == "verification_error"
    assert classify_failure(wrong_tool_selection) == "wrong_tool_selection"


@pytest.mark.asyncio
async def test_benchmark_mcp_is_real_stdio_discovery_and_call() -> None:
    config = MCPServerConfig(
        server_id="benchmark",
        transport="stdio",
        command=sys.executable,
        args=[str(Path("benchmarks/fixtures/mcp_server.py").resolve())],
        trust_tool_annotations=True,
    )
    registry = ToolRegistry()
    async with MCPToolProvider([config]) as provider:
        await provider.register_tools(registry)
        discovered = {item.remote_tool_name for item in provider.discovery_info}
        result = await registry.invoke(
            "benchmark__lookup_record", {"product_id": "p100"}
        )
    assert {"lookup_candidates", "lookup_record", "source_a", "source_b"} <= discovered
    assert result["structured_content"] == {
        "product_id": "p100",
        "name": "Atlas Keyboard",
        "price": 699,
        "stock": 8,
    }


def test_fixture_server_external_state_oracle_roundtrip() -> None:
    with FixtureServer() as fixture:
        fixture.reset()
        configured = task(
            id="fixture",
            oracle_type=OracleType.FIXTURE_STATE,
            oracle_config={
                "url": f"{fixture.base_url}/fixture-state",
                "checks": [
                    {"name": "no_submit", "path": "submit_count", "contains": 0}
                ],
            },
        )
        result = evaluate_oracle(configured, {})
    assert result.passed is True
