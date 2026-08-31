"""TaskPilot 结构化模型测试。"""

from taskpilot.models import PlanStep, PlanStepStatus, TaskSpec, VerificationResult


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
