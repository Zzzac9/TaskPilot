"""Phase14 cross-functional benchmark.

Runs deterministic, multi-step pytest/Chromium/MCP scenarios and writes a machine-
readable coverage report. Real-LLM end-to-end cases are separate in
benchmarks/phase14_real_llm.py so mocks never inflate a real-model success rate.
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]

# Every functional boundary has existing tests plus the new state-machine suite.
COVERAGE = {
    "requirement_contract_and_optional_downgrade": ["test_phase14_contract_benchmark.py", "test_planner.py", "test_analyzer.py"],
    "verifier_truthfulness_and_partial_results": ["test_verifier.py", "test_finalizer.py"],
    "failure_analysis_and_bounded_replanning": ["test_replanner.py", "test_phase10_graph.py"],
    "global_budget_and_crash_recovery": ["test_phase9_recovery.py", "test_phase9_persistence.py"],
    "policy_approval_and_human_assistance": ["test_browser_policy.py", "test_hitl.py", "test_human_assistance.py"],
    "real_chromium_dom_and_vision": ["test_browser.py", "test_phase11_vision.py"],
    "mcp_and_filesystem_artifacts": ["test_mcp_integration.py", "test_phase13_filesystem_mcp.py"],
    "oracle_and_benchmark_invariants": ["test_phase12_benchmark.py", "test_phase13_real_llm_harness.py"],
}

CASES = sorted({name for group in COVERAGE.values() for name in group})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, help="Location for report folder")
    args = parser.parse_args()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = (args.output or ROOT / "benchmarks" / "results" / f"{timestamp}-phase14-contract").resolve()
    out.mkdir(parents=True, exist_ok=True)
    junit = out / "pytest.xml"

    command = [
        sys.executable, "-m", "pytest", "-q",
        *(str(ROOT / "tests" / name) for name in CASES),
        "--junitxml", str(junit),
    ]
    result = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", check=False)
    (out / "pytest.log").write_text(result.stdout, encoding="utf-8")

    counts: Counter[str] = Counter()
    per_file: dict[str, Counter[str]] = {name: Counter() for name in CASES}
    if junit.exists():
        xml = ElementTree.parse(junit)
        for case in xml.findall(".//testcase"):
            classname = case.get("classname") or ""
            file_name = classname.rsplit(".", 1)[-1] + ".py"
            outcome = "passed"
            if case.find("failure") is not None or case.find("error") is not None:
                outcome = "failed"
            elif case.find("skipped") is not None:
                outcome = "skipped"
            counts[outcome] += 1
            if file_name in per_file:
                per_file[file_name][outcome] += 1

    coverage = {
        name: {
            "test_modules": filenames,
            "passed": sum(per_file[m]["passed"] for m in filenames),
            "failed": sum(per_file[m]["failed"] for m in filenames),
            "skipped": sum(per_file[m]["skipped"] for m in filenames),
        }
        for name, filenames in COVERAGE.items()
    }
    missing = sorted(name for name, entry in coverage.items()
                     if entry["passed"] == 0 or entry["failed"] > 0)
    passed = (result.returncode == 0 and not missing and counts["passed"] > 0)

    summary = {
        "kind": "offline-regression-benchmark",
        "passed": passed, "counts": dict(counts),
        "coverage": coverage, "missing_or_failed_coverage": missing,
        "exit_code": result.returncode,
        "python": sys.version, "platform": platform.platform(),
        "git_sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                  capture_output=True, text=True, check=False).stdout.strip(),
        "real_llm_tests_included": False,
        "real_model_only_runner": "benchmarks/phase14_real_llm.py",
        "limitations": [
            "自然语言语义等价性不能仅凭要求 ID 的映射被数学证明",
            "真实模型成功率需要独立于 pytest 统计",
            "只读历史观察可复用，但时效性仍需 Verifier 判断",
        ],
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    lines = [
        "# Phase 14: Contract, Recovery and Safety Benchmark", "",
        f"**Result:** {'PASS' if passed else 'FAIL'}",
        f"**Cases:** {counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped",
        "",
        "| Functional boundary | Passed | Failed | Skipped |", "|---|---:|---:|---:|",
    ]
    for name, item in coverage.items():
        lines.append(f"| {name} | {item['passed']} | {item['failed']} | {item['skipped']} |")
    lines.extend([
        "", "**Oracles:** pytest assertions, real Chromium fixture responses,",
        "MCP protocol responses, approval and side-effect state, checkpoint recovery,",
        "OOXML artifact inspection, Graph budget counters and terminal status.",
        "",
        "**Important:** This is deterministic regression coverage, not real LLM task success.",
    ])
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("BENCHMARK_RESULT", "PASS" if passed else "FAIL")
    print("BENCHMARK_CASES", dict(counts))
    print("BENCHMARK_REPORT", str(out / "report.md"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
