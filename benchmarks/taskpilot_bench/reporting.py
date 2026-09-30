"""不可覆盖 raw artifacts、CSV 与 Markdown 报告。"""

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from benchmarks.taskpilot_bench.environment import redact_secrets
from benchmarks.taskpilot_bench.models import (
    BenchmarkRunRecord,
    BenchmarkSummary,
    ReliabilitySummary,
)


def write_json_exclusive(path: Path, value: Any) -> None:
    """用 x mode 保证前一轮 benchmark artifact 不会被覆盖。"""

    safe = redact_secrets(value)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(safe, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")


def write_runs_jsonl(path: Path, records: Iterable[BenchmarkRunRecord]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for record in records:
            value = redact_secrets(record.model_dump(mode="json"))
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def read_runs_jsonl(path: Path) -> list[BenchmarkRunRecord]:
    records: list[BenchmarkRunRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(BenchmarkRunRecord.model_validate_json(line))
    return records


def write_summary_csv(path: Path, summary: BenchmarkSummary) -> None:
    rows = [
        ("declared_tasks", summary.declared_tasks),
        ("eligible_tasks", summary.eligible_tasks),
        ("not_run_tasks", summary.not_run_tasks),
        ("executed_tasks", summary.executed_tasks),
        ("succeeded_tasks", summary.succeeded_tasks),
        ("failed_tasks", summary.failed_tasks),
        ("task_success_rate_percent", summary.task_success_rate.percentage),
        ("false_completion_rate_percent", summary.false_completion_rate.percentage),
        ("average_tool_calls_per_task", summary.average_tool_calls_per_task),
        (
            "average_tool_calls_per_successful_task",
            summary.average_tool_calls_per_successful_task,
        ),
        ("tool_failure_rate_percent", summary.tool_failure_rate.percentage),
        ("average_replans_per_task", summary.average_replans_per_task),
        ("replan_trigger_rate_percent", summary.replan_trigger_rate.percentage),
        ("replan_recovery_rate_percent", summary.replan_recovery_rate.percentage),
        ("step_rejection_rate_percent", summary.step_rejection_rate.percentage),
        ("approval_request_rate_percent", summary.approval_request_rate.percentage),
        ("mean_wall_latency_ms", summary.mean_wall_latency_ms),
        ("median_wall_latency_ms", summary.median_wall_latency_ms),
        ("p95_wall_latency_ms", summary.p95_wall_latency_ms),
    ]
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        writer.writerows(rows)


def render_report(
    summary: BenchmarkSummary,
    *,
    environment: dict[str, Any],
    failures: list[BenchmarkRunRecord],
    reliability: ReliabilitySummary | None,
    replan_evaluation: dict[str, Any] | None,
    stability: dict[str, Any] | None,
    raw_path: str,
) -> str:
    """生成强调 denominator、oracle 和未运行能力的工程报告。"""

    success = summary.task_success_rate
    interval = success.confidence_95
    interval_text = (
        f"[{interval.lower * 100:.2f}%, {interval.upper * 100:.2f}%]"
        if interval is not None
        else "unavailable"
    )
    category_lines = (
        "\n".join(
            f"- {item.category.value}: {item.success.numerator}/{item.success.denominator} "
            f"({item.success.percentage:.2f}%)"
            for item in summary.category_results
            if item.success.percentage is not None
        )
        or "- No executed categories."
    )

    def failure_detail(item: BenchmarkRunRecord) -> str:
        if item.error:
            return item.error
        if item.oracle is not None:
            return item.oracle.reason
        return "oracle unavailable"

    failure_lines = (
        "\n".join(
            f"- {item.task_id}: {item.failure_category or 'unknown'} — {failure_detail(item)}"
            for item in failures
        )
        or "- None."
    )
    reliability_text = (
        "NOT RUN"
        if reliability is None
        else json.dumps(
            reliability.model_dump(mode="json"), ensure_ascii=False, indent=2
        )
    )
    return f"""# TaskPilot Benchmark Report

## Environment

- Python: {environment.get("python_version")}
- Git commit: {environment.get("taskpilot_git_commit")}
- Git dirty: {environment.get("git_dirty")}
- Model provider: {environment.get("model_provider")}
- Model: {environment.get("model_name")}

## Task Set and Denominator

- Tasks declared in this run: {summary.declared_tasks}
- Eligible: {summary.eligible_tasks}
- NOT_RUN because capability unavailable: {summary.not_run_tasks}
- Executed: {summary.executed_tasks}
- Succeeded: {summary.succeeded_tasks}
- Failed: {summary.failed_tasks}
- Invariant: S + F == Z is {summary.succeeded_tasks + summary.failed_tasks == summary.executed_tasks}

## Primary Results

- Task Success Rate: {success.numerator}/{success.denominator} ({success.percentage if success.percentage is not None else "unavailable"}%)
- 95% Wilson interval: {interval_text}
- False Completion: {summary.false_completion_rate.numerator}/{summary.false_completion_rate.denominator}
- Average tool calls/task: {summary.average_tool_calls_per_task}
- Average tool calls/successful task: {summary.average_tool_calls_per_successful_task}
- Tool failure rate: {summary.tool_failure_rate.percentage}%
- Mean/median/P95 latency (ms): {summary.mean_wall_latency_ms} / {summary.median_wall_latency_ms} / {summary.p95_wall_latency_ms}

## Category Results

{category_lines}

## Failure Cases

{failure_lines}

## Replanning Evaluation

{json.dumps(replan_evaluation, ensure_ascii=False, indent=2) if replan_evaluation else "NOT RUN"}

## Vision Evaluation

Real external vision-model ablation = NOT RUN unless a separate vision run artifact is referenced.
Deterministic FakeVisionTargeter reliability cases are never reported as model success improvement.

## Runtime Reliability and HITL Safety

```json
{reliability_text}
```

## Context Measurement

Only character-context reduction may be reported. No tokenizer-based reduction is claimed.

## Stability Subset

{json.dumps(stability, ensure_ascii=False, indent=2) if stability else "NOT RUN"}

## Limitations

- Localhost fixtures only; no production public-web benchmark.
- External oracle is distinct from TaskPilot Verifier.
- Trace is audit data and is not assumed exactly-once.
- Token usage remains null when provider usage metadata is unavailable.

## Raw Results Location

{raw_path}
"""
