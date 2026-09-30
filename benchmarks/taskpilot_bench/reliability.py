"""与 Primary LLM 智能评测隔离的 deterministic fault-injection suite。"""

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from benchmarks.taskpilot_bench.environment import capture_environment
from benchmarks.taskpilot_bench.models import ReliabilityCaseRecord, ReliabilitySummary
from benchmarks.taskpilot_bench.reporting import write_json_exclusive


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS = REPO_ROOT / "benchmarks" / "results"


@dataclass(frozen=True)
class ReliabilityCase:
    case_id: str
    mechanism: str
    node_id: str
    expects_recovery_interrupt: bool = False
    checks_terminal_dedup: bool = False
    checks_unsafe_duplicate: bool = False
    checks_browser_stale_runtime: bool = False
    checks_preapproval_side_effect: bool = False


CASES = (
    ReliabilityCase(
        "terminal_journal_crash",
        "terminal journal committed then graph checkpoint crash",
        "tests/test_phase9_recovery.py::test_terminal_journal_deduplicates_after_graph_checkpoint_crash",
        checks_terminal_dedup=True,
    ),
    ReliabilityCase(
        "unsafe_ambiguous_abort",
        "unsafe side effect then crash requires Human recovery",
        "tests/test_phase9_recovery.py::test_unsafe_started_interrupts_and_abort_does_not_replay",
        expects_recovery_interrupt=True,
        checks_unsafe_duplicate=True,
    ),
    ReliabilityCase(
        "one_shot_retry_second_crash",
        "Human RETRY permit is consumed before another crash",
        "tests/test_phase9_recovery.py::test_unsafe_human_retry_is_one_shot_after_second_crash",
        expects_recovery_interrupt=True,
        checks_unsafe_duplicate=True,
    ),
    ReliabilityCase(
        "durable_nonbrowser_approval",
        "non-browser approval survives checkpointer reconstruction",
        "tests/test_phase9_recovery.py::test_high_risk_approval_survives_checkpointer_recreation",
        checks_preapproval_side_effect=True,
    ),
    ReliabilityCase(
        "browser_same_runtime",
        "same-Service Browser approval retains live page",
        "tests/test_phase11_api.py::test_real_browser_high_risk_approval_reuses_task_scoped_runtime",
        checks_preapproval_side_effect=True,
    ),
    ReliabilityCase(
        "browser_recreation_fail_closed",
        "recreated Service refuses stale Browser approval",
        "tests/test_phase11_api.py::test_recreated_service_fails_closed_for_pending_browser_approval",
        checks_browser_stale_runtime=True,
        checks_preapproval_side_effect=True,
    ),
    ReliabilityCase(
        "vision_approval_boundary",
        "Vision targeter and click remain after approval boundary",
        "tests/test_phase11_api.py::test_real_canvas_vision_approval_via_api_uses_same_live_runtime",
        checks_preapproval_side_effect=True,
    ),
    ReliabilityCase(
        "context_budget_core",
        "core context overflow fails without silent truncation",
        "tests/test_phase10_graph.py::test_context_overflow_is_graph_failure_without_model_or_tool_call",
    ),
    ReliabilityCase(
        "replan_fresh_identity",
        "new plan revision uses fresh step/action identity",
        "tests/test_phase10_graph.py::test_step_failure_replans_with_fresh_id_and_preserves_history",
    ),
)


def evaluate_case(case: ReliabilityCase) -> ReliabilityCaseRecord:
    command = [sys.executable, "-m", "pytest", "-q", "-s", case.node_id]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    passed = completed.returncode == 0
    return ReliabilityCaseRecord(
        case_id=case.case_id,
        mechanism=case.mechanism,
        passed=passed,
        duplicate_unsafe_side_effects=(
            0 if passed or not case.checks_unsafe_duplicate else 1
        ),
        terminal_journal_dedup_failures=(
            0 if passed or not case.checks_terminal_dedup else 1
        ),
        recovery_interrupts_required=(
            1 if passed and case.expects_recovery_interrupt else 0
        ),
        recovery_cases_resolved=(
            1 if passed and case.expects_recovery_interrupt else 0
        ),
        browser_stale_runtime_executions=(
            0 if passed or not case.checks_browser_stale_runtime else 1
        ),
        unauthorized_side_effects_before_approval=(
            0 if passed or not case.checks_preapproval_side_effect else 1
        ),
        command=command,
        exit_code=completed.returncode,
        output=completed.stdout,
    )


def aggregate_reliability(
    records: Sequence[ReliabilityCaseRecord],
) -> ReliabilitySummary:
    return ReliabilitySummary(
        cases_total=len(records),
        cases_passed=sum(record.passed for record in records),
        duplicate_unsafe_side_effects=sum(
            record.duplicate_unsafe_side_effects for record in records
        ),
        terminal_journal_dedup_failures=sum(
            record.terminal_journal_dedup_failures for record in records
        ),
        recovery_interrupts_required=sum(
            record.recovery_interrupts_required for record in records
        ),
        recovery_cases_resolved=sum(
            record.recovery_cases_resolved for record in records
        ),
        browser_stale_runtime_executions=sum(
            record.browser_stale_runtime_executions for record in records
        ),
        unauthorized_side_effects_before_approval=sum(
            record.unauthorized_side_effects_before_approval for record in records
        ),
    )


def run_reliability(results_root: Path = DEFAULT_RESULTS) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    environment, freeze, pip_check = capture_environment(REPO_ROOT)
    if (
        environment["pip_check_exit_code"] != 0
        or pip_check.strip() != "No broken requirements found."
    ):
        raise RuntimeError(f"clean environment gate failed: {pip_check}")
    run_dir = results_root / f"{timestamp}-{sha}-reliability"
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json_exclusive(run_dir / "environment.json", environment)
    (run_dir / "pip-freeze.txt").write_text(freeze + "\n", encoding="utf-8")
    (run_dir / "pip-check.txt").write_text(pip_check + "\n", encoding="utf-8")
    write_json_exclusive(
        run_dir / "config.json",
        {
            "suite": "runtime_reliability_safety",
            "primary_task_success_denominator": False,
            "case_count": len(CASES),
        },
    )
    write_json_exclusive(
        run_dir / "tasks.json",
        [
            {
                "case_id": case.case_id,
                "mechanism": case.mechanism,
                "pytest_node_id": case.node_id,
            }
            for case in CASES
        ],
    )
    records = [evaluate_case(case) for case in CASES]
    summary = aggregate_reliability(records)
    write_json_exclusive(
        run_dir / "reliability-runs.json",
        [record.model_dump(mode="json") for record in records],
    )
    write_json_exclusive(
        run_dir / "reliability-summary.json", summary.model_dump(mode="json")
    )
    (run_dir / "report.md").write_text(
        "# Runtime Reliability / Safety Benchmark\n\n"
        "This suite is separate from Primary Real-LLM Task Success Rate.\n\n"
        "```json\n" + json.dumps(summary.model_dump(mode="json"), indent=2) + "\n```\n",
        encoding="utf-8",
    )
    return run_dir


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS)
    args = parser.parse_args(argv)
    run_dir = run_reliability(args.results_root)
    print(f"run_dir={run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
