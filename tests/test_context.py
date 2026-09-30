"""Phase 10 deterministic Context Builder 离线测试。"""

import json

import pytest

from taskpilot.context import ContextBudgetError, ContextBuilder, ContextConfig
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    ReplanRequest,
    ReplanTrigger,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    ToolCallRecord,
    ToolCallStatus,
)


def spec(goal: str = "Find verified candidates") -> TaskSpec:
    return TaskSpec(goal=goal, constraints={"count": 2}, completion_criteria=["done"])


def step() -> PlanStep:
    return PlanStep(
        id=3,
        description="Verify candidates",
        depends_on=[1],
        success_criteria=["Two candidates verified"],
    )


def call(number: int, result: object | None = None) -> ToolCallRecord:
    return ToolCallRecord(
        call_id=f"call-{number}",
        step_id=3,
        tool_name="lookup",
        status=ToolCallStatus.SUCCESS,
        result=result if result is not None else {"number": number},
    )


def test_context_preserves_core_fields() -> None:
    snapshot = ContextBuilder().build_executor_context(
        task_spec=spec(), step=step(), task_results={}, tool_calls=[]
    )
    assert snapshot.content["task_goal"] == "Find verified candidates"
    assert snapshot.content["task_constraints"] == {"count": 2}
    assert snapshot.content["current_step"]["success_criteria"] == [
        "Two candidates verified"
    ]


def test_context_only_includes_latest_tool_calls() -> None:
    builder = ContextBuilder(ContextConfig(max_recent_tool_calls=2))
    snapshot = builder.build_executor_context(
        task_spec=spec(), step=step(), task_results={}, tool_calls=[call(i) for i in range(5)]
    )
    assert [item["call_id"] for item in snapshot.content["current_step_observations"]] == [
        "call-3",
        "call-4",
    ]
    assert snapshot.included_tool_calls == 2
    assert snapshot.omitted_tool_calls == 3


def test_large_observation_has_explicit_truncation_marker() -> None:
    builder = ContextBuilder(ContextConfig(max_single_observation_chars=20))
    snapshot = builder.build_executor_context(
        task_spec=spec(), step=step(), task_results={}, tool_calls=[call(1, "x" * 200)]
    )
    observation = snapshot.content["current_step_observations"][0]["observation"]
    assert observation["truncated"] is True
    assert observation["original_length"] > 20
    assert "tool_observation:call-1" in snapshot.truncated_sections
    print(
        "CONTEXT_BUDGET_SMOKE="
        + json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    )


def test_verified_dependency_result_precedes_raw_history() -> None:
    builder = ContextBuilder(
        ContextConfig(max_total_chars=900, max_single_observation_chars=500)
    )
    dependency = StepResult(step_id=1, summary="verified", output={"facts": ["A", "B"]})
    snapshot = builder.build_executor_context(
        task_spec=spec(),
        step=step(),
        task_results={},
        tool_calls=[call(1, "x" * 500), call(2, "y" * 500)],
        step_results=[dependency],
    )
    assert snapshot.content["dependency_results"][0]["summary"] == "verified"
    assert snapshot.omitted_tool_calls >= 1


def test_feedback_is_kept_for_matching_step() -> None:
    feedback = StepVerificationResult(
        step_id=3,
        verified=False,
        checks=[CriterionCheck(criterion_index=0, satisfied=False, reason="missing")],
        feedback="Need a second source",
    )
    snapshot = ContextBuilder().build_executor_context(
        task_spec=spec(),
        step=step(),
        task_results={},
        tool_calls=[],
        verification_feedback=feedback,
    )
    assert snapshot.content["verification_feedback"]["feedback"] == "Need a second source"


def test_feedback_from_other_step_is_not_included() -> None:
    feedback = StepVerificationResult(step_id=99, verified=False, feedback="irrelevant")
    snapshot = ContextBuilder().build_executor_context(
        task_spec=spec(), step=step(), task_results={}, tool_calls=[], verification_feedback=feedback
    )
    assert snapshot.content["verification_feedback"] is None


def test_core_budget_overflow_is_explicit() -> None:
    builder = ContextBuilder(ContextConfig(max_total_chars=50))
    with pytest.raises(ContextBudgetError, match="核心上下文"):
        builder.build_executor_context(
            task_spec=spec("g" * 200), step=step(), task_results={}, tool_calls=[]
        )


def test_context_does_not_mutate_durable_history() -> None:
    records = [call(1, {"items": [1, 2]})]
    before = records[0].model_dump_json()
    ContextBuilder(ContextConfig(max_single_observation_chars=5)).build_executor_context(
        task_spec=spec(), step=step(), task_results={}, tool_calls=records
    )
    assert records[0].model_dump_json() == before


def test_replan_context_contains_plan_and_verified_facts() -> None:
    request = ReplanRequest(
        trigger=ReplanTrigger.STEP_ATTEMPTS_EXHAUSTED,
        failed_step_id=3,
        reason="path blocked",
        verification_feedback="missing evidence",
    )
    snapshot = ContextBuilder().build_replan_context(
        task_spec=spec(),
        plan=[PlanStep(id=1, description="done", success_criteria=["done"])],
        step_results=[StepResult(step_id=1, summary="fact")],
        tool_calls=[call(1)],
        request=request,
        plan_revision=0,
        replan_count=0,
    )
    assert snapshot.content["replan_trigger"] == "step_attempts_exhausted"
    assert snapshot.content["verified_step_results"][0]["summary"] == "fact"
    assert snapshot.content["historical_max_step_id"] == 1


def test_replan_context_redacts_tool_argument_secrets() -> None:
    record = call(1)
    record.arguments = {"api_key": "secret-value", "query": "safe"}
    snapshot = ContextBuilder().build_replan_context(
        task_spec=spec(),
        plan=[step()],
        step_results=[],
        tool_calls=[record],
        request=ReplanRequest(
            trigger=ReplanTrigger.STEP_ATTEMPTS_EXHAUSTED,
            failed_step_id=3,
            reason="failed",
        ),
        plan_revision=0,
        replan_count=0,
    )
    dumped = json.dumps(snapshot.content, ensure_ascii=False)
    assert "secret-value" not in dumped
    assert "[REDACTED]" in dumped


@pytest.mark.parametrize(
    "field",
    [
        "max_total_chars",
        "max_single_observation_chars",
        "max_dependency_result_chars",
        "max_task_results_chars",
        "max_feedback_chars",
    ],
)
def test_positive_context_limits_are_validated(field: str) -> None:
    with pytest.raises(ValueError):
        ContextConfig(**{field: 0})


def test_zero_recent_tool_calls_is_supported() -> None:
    snapshot = ContextBuilder(ContextConfig(max_recent_tool_calls=0)).build_executor_context(
        task_spec=spec(), step=step(), task_results={}, tool_calls=[call(1)]
    )
    assert snapshot.included_tool_calls == 0
    assert snapshot.omitted_tool_calls == 1


def test_context_budget_smoke_with_twenty_historical_calls() -> None:
    dependency = StepResult(step_id=1, summary="verified dependency", output={"fact": "kept"})
    snapshot = ContextBuilder(
        ContextConfig(max_recent_tool_calls=5, max_single_observation_chars=40)
    ).build_executor_context(
        task_spec=spec(),
        step=step(),
        task_results={},
        tool_calls=[call(index, "x" * 100) for index in range(20)],
        step_results=[dependency],
    )
    assert snapshot.included_tool_calls == 5
    assert snapshot.omitted_tool_calls == 15
    print(
        "CONTEXT_BUDGET_SMOKE="
        + json.dumps(
            {
                "historical_tool_calls": 20,
                "included_raw_tool_calls": snapshot.included_tool_calls,
                "goal_preserved": snapshot.content["task_goal"] == spec().goal,
                "dependency_result_preserved": bool(snapshot.content["dependency_results"]),
                "truncated_sections": snapshot.truncated_sections,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
