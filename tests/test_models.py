"""TaskPilot 结构化模型测试。"""

from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    PlanStepStatus,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    VerificationResult,
)


def test_task_spec_can_be_created() -> None:
    spec = TaskSpec(
        goal="Generate a CSV of matching gyms",
        constraints={"monthly_fee_max": 500},
        expected_output="CSV",
        completion_criteria=["Contains three gyms"],
    )

    assert spec.goal == "Generate a CSV of matching gyms"
    assert spec.constraints == {"monthly_fee_max": 500}
    assert spec.expected_output == "CSV"


def test_plan_step_defaults() -> None:
    step = PlanStep(id=1, description="Find candidate gyms")

    assert step.status is PlanStepStatus.PENDING
    assert step.retry_count == 0
    assert step.depends_on == []
    assert step.success_criteria == []


def test_mutable_defaults_are_not_shared() -> None:
    first_spec = TaskSpec(goal="First")
    second_spec = TaskSpec(goal="Second")
    first_step = PlanStep(id=1, description="First")
    second_step = PlanStep(id=2, description="Second")

    first_spec.constraints["region"] = "Hong Kong Island"
    first_spec.completion_criteria.append("Complete")
    first_step.depends_on.append(99)
    first_step.success_criteria.append("First step is complete")

    assert second_spec.constraints == {}
    assert second_spec.completion_criteria == []
    assert second_step.depends_on == []
    assert second_step.success_criteria == []


def test_verification_result_can_be_created() -> None:
    result = VerificationResult(
        completed=False,
        reason="One requirement remains",
        missing_requirements=["CSV output"],
        next_action="Generate the CSV",
    )

    assert result.completed is False
    assert result.missing_requirements == ["CSV output"]
    assert result.next_action == "Generate the CSV"


def test_step_execution_outcome_evidence_is_not_shared() -> None:
    first = StepExecutionOutcome(step_id=1)
    second = StepExecutionOutcome(step_id=2)

    first.evidence.append("offline evidence")
    first.output["candidate"] = "A"

    assert first.claimed_complete is False
    assert first.action_count == 0
    assert first.evidence == ["offline evidence"]
    assert second.evidence == []
    assert second.output == {}


def test_step_result_mutable_defaults_are_not_shared() -> None:
    first = StepResult(step_id=1, summary="First")
    second = StepResult(step_id=2, summary="Second")

    first.output["items"] = ["A"]
    first.evidence.append("Evidence A")

    assert second.output == {}
    assert second.evidence == []


def test_step_verification_result_can_be_created() -> None:
    result = StepVerificationResult(
        step_id=1,
        verified=True,
        checks=[
            CriterionCheck(
                criterion_index=0,
                satisfied=True,
                reason="Evidence is present",
            )
        ],
    )

    assert result.step_id == 1
    assert result.verified is True
    assert result.checks[0].criterion_index == 0
