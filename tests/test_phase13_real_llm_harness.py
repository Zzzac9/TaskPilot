"""Deterministic tests for the independent Phase 13 real-LLM oracle."""

from pathlib import Path

from benchmarks.phase13_real_llm import (
    EXPECTED_FACTS,
    EXPECTED_TITLE,
    evaluate_document,
    summarize,
)
from taskpilot.filesystem_mcp import SandboxedFilesystem


def test_phase13_oracle_reads_real_docx_content_and_source(tmp_path: Path) -> None:
    source_url = "http://127.0.0.1:4321/details.html"
    SandboxedFilesystem(tmp_path).generate_docx(
        "gym-research.docx",
        EXPECTED_TITLE,
        list(EXPECTED_FACTS),
        [source_url],
    )

    oracle = evaluate_document(tmp_path / "gym-research.docx", source_url)

    assert oracle["passed"] is True
    assert all(oracle["checks"].values())


def test_phase13_summary_counts_completed_oracle_failure_as_false_completion() -> None:
    summary = summarize(
        [
            {"success": True, "false_completion": False},
            {"success": False, "false_completion": True},
            {"success": False, "false_completion": False},
        ],
        source_url="http://127.0.0.1:4321/details.html",
    )

    assert summary["successes"] == 1
    assert summary["success_rate"] == 1 / 3
    assert summary["false_completions"] == 1
    assert summary["false_completion_rate"] == 1 / 3
