"""从不可变 run artifacts 生成 Phase 12 最终评价汇总。"""

import argparse
import json
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from benchmarks.taskpilot_bench.environment import capture_environment, redact_secrets
from benchmarks.taskpilot_bench.loader import load_manifest
from benchmarks.taskpilot_bench.metrics import (
    aggregate_runs,
    replan_ablation,
    stability_summary,
)
from benchmarks.taskpilot_bench.models import (
    BenchmarkCategory,
    BenchmarkRunRecord,
    ReliabilitySummary,
)
from benchmarks.taskpilot_bench.reporting import read_runs_jsonl, write_json_exclusive


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS = REPO_ROOT / "benchmarks" / "results"
DEFAULT_MANIFEST = REPO_ROOT / "benchmarks" / "manifest.json"


def classify_failure(record: BenchmarkRunRecord) -> str | None:
    """在不改 raw success/status/oracle 的前提下归类主要失败原因。"""

    if record.success:
        return None
    if record.false_completion:
        return "verification_error"
    if (
        record.task_status == "failed"
        and record.oracle_passed is True
        and record.approval_request_count > 0
    ):
        return "human_policy_reject"
    observed = record.oracle.observed_output if record.oracle is not None else None
    state_error = observed.get("error") if isinstance(observed, dict) else None
    text = f"{record.error or ''}\n{state_error or ''}".casefold()
    if "timeout" in text:
        return "timeout"
    if "task analyzer" in text or "task planner" in text:
        return "planning_error"
    if "tool call" in text and ("实际数量" in text or "actual" in text):
        return "wrong_tool_selection"
    if "step verifier" in text or "task verifier" in text:
        return "verification_error"
    if "locator" in text or "browser" in text:
        return "browser_locator_error"
    if "context" in text:
        return "context_error"
    if record.tool_failure_count > 0:
        return "mcp_error" if record.category in {
            BenchmarkCategory.MCP,
            BenchmarkCategory.REPLANNING,
        } else "unknown"
    if record.error:
        return "environment_error"
    return "unknown"


def _run_id(records: Sequence[BenchmarkRunRecord]) -> str:
    values = {record.run_id for record in records}
    if len(values) != 1:
        raise ValueError("source JSONL 必须只包含一个 run_id")
    return next(iter(values))


def _failure_analysis(records: Sequence[BenchmarkRunRecord]) -> dict[str, Any]:
    items = [
        {
            "task_id": record.task_id,
            "category": classify_failure(record),
            "task_status": record.task_status,
            "oracle_passed": record.oracle_passed,
            "false_completion": record.false_completion,
        }
        for record in records
        if not record.success
    ]
    counts = Counter(str(item["category"]) for item in items)
    return {"counts": dict(sorted(counts.items())), "items": items}


def finalize(
    *,
    primary_dir: Path,
    no_replan_dir: Path,
    stability_2_dir: Path,
    stability_3_dir: Path,
    reliability_dir: Path,
    results_root: Path = DEFAULT_RESULTS,
) -> Path:
    """校验 source run 关系并写入新的 final evaluation artifact。"""

    primary = read_runs_jsonl(primary_dir / "runs.jsonl")
    no_replan = read_runs_jsonl(no_replan_dir / "runs.jsonl")
    stability_2 = read_runs_jsonl(stability_2_dir / "runs.jsonl")
    stability_3 = read_runs_jsonl(stability_3_dir / "runs.jsonl")
    if len(primary) != 30 or len(no_replan) != 4:
        raise ValueError("Primary 必须 30 题且 No-Replan 必须 4 题")
    primary_summary = aggregate_runs(_run_id(primary), primary, declared_tasks=30)
    if primary_summary.executed_tasks != 30:
        raise ValueError("Primary 必须有 30 个 executed tasks")

    no_replan_ids = {record.task_id for record in no_replan}
    full_replan = [record for record in primary if record.task_id in no_replan_ids]
    ablation = replan_ablation(full_replan, no_replan)

    manifest = load_manifest(DEFAULT_MANIFEST)
    stability_ids = {task.id for task in manifest if "stability" in task.tags}
    stability_1 = [record for record in primary if record.task_id in stability_ids]
    for expected_attempt, records in ((2, stability_2), (3, stability_3)):
        if {record.task_id for record in records} != stability_ids:
            raise ValueError("stability run task IDs 与冻结 subset 不一致")
        if {record.attempt for record in records} != {expected_attempt}:
            raise ValueError("stability run attempt 编号不一致")
    stability = stability_summary(stability_1 + stability_2 + stability_3)

    reliability = ReliabilitySummary.model_validate_json(
        (reliability_dir / "reliability-summary.json").read_text(encoding="utf-8")
    )
    sources = {
        "primary": _run_id(primary),
        "no_replan": _run_id(no_replan),
        "stability_2": _run_id(stability_2),
        "stability_3": _run_id(stability_3),
        "reliability": reliability_dir.name,
    }
    summary = {
        "source_runs": sources,
        "primary": primary_summary.model_dump(mode="json"),
        "failure_analysis": _failure_analysis(primary),
        "replan_ablation": ablation,
        "stability": stability,
        "runtime_reliability": reliability.model_dump(mode="json"),
        "vision_ablation": "NOT_RUN",
        "token_reduction_claim": None,
    }

    environment, freeze, pip_check = capture_environment(REPO_ROOT)
    if pip_check.strip() != "No broken requirements found.":
        raise RuntimeError(f"clean environment gate failed: {pip_check}")
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = results_root / f"{timestamp}-{sha}-evaluation"
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json_exclusive(run_dir / "environment.json", environment)
    write_json_exclusive(run_dir / "evaluation-summary.json", summary)
    (run_dir / "pip-freeze.txt").write_text(freeze + "\n", encoding="utf-8")
    (run_dir / "pip-check.txt").write_text(pip_check + "\n", encoding="utf-8")

    task_rate = primary_summary.task_success_rate
    interval = task_rate.confidence_95
    failure_lines = "\n".join(
        f"- {name}: {count}"
        for name, count in summary["failure_analysis"]["counts"].items()
    )
    report = f"""# TaskPilot Phase 12 Final Evaluation

## Source Runs

```json
{json.dumps(sources, ensure_ascii=False, indent=2)}
```

## Primary

- declared / eligible / executed: 30 / 30 / 30
- succeeded / failed: {primary_summary.succeeded_tasks} / {primary_summary.failed_tasks}
- Task Success Rate: {task_rate.numerator}/{task_rate.denominator} ({task_rate.percentage:.2f}%)
- Wilson 95% CI: [{interval.lower * 100:.2f}%, {interval.upper * 100:.2f}%]
- False Completion: {primary_summary.false_completion_rate.numerator}/30
- Average Tool calls/task: {primary_summary.average_tool_calls_per_task}
- Tool failure rate: {primary_summary.tool_failure_rate.percentage:.2f}%
- Replan recovery: {primary_summary.replan_recovery_rate.numerator}/{primary_summary.replan_recovery_rate.denominator}

## Failure Analysis

{failure_lines}

## Replanning Ablation

```json
{json.dumps(ablation, ensure_ascii=False, indent=2)}
```

## Stability

```json
{json.dumps(stability, ensure_ascii=False, indent=2)}
```

## Runtime Reliability

```json
{json.dumps(reliability.model_dump(mode="json"), ensure_ascii=False, indent=2)}
```

## Boundaries

- Real external Vision model ablation: NOT RUN.
- Token reduction: not measured and not claimed.
- Localhost fixtures only; no public-web production benchmark.
- Runtime reliability cases do not prove universal exactly-once.
"""
    (run_dir / "report.md").write_text(
        str(redact_secrets(report)), encoding="utf-8"
    )
    return run_dir


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary", type=Path, required=True)
    parser.add_argument("--no-replan", type=Path, required=True)
    parser.add_argument("--stability-2", type=Path, required=True)
    parser.add_argument("--stability-3", type=Path, required=True)
    parser.add_argument("--reliability", type=Path, required=True)
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS)
    args = parser.parse_args(argv)
    run_dir = finalize(
        primary_dir=args.primary,
        no_replan_dir=args.no_replan,
        stability_2_dir=args.stability_2,
        stability_3_dir=args.stability_3,
        reliability_dir=args.reliability,
        results_root=args.results_root,
    )
    print(f"run_dir={run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
