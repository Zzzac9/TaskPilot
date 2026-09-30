"""Phase 5 Step/Task Verifier 的确定性与离线测试。"""

from unittest.mock import Mock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel

from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    VerificationResult,
)
from taskpilot.verifier import (
    STEP_VERIFIER_SYSTEM_PROMPT,
    TASK_VERIFIER_SYSTEM_PROMPT,
    TaskVerifier,
    validate_step_verification,
    validate_task_verification,
)


def make_step() -> PlanStep:
    return PlanStep(
        id=2,
        description="整理候选",
        depends_on=[1],
        success_criteria=["包含两个候选", "每个候选都有名称"],
    )


def make_valid_step_verification() -> StepVerificationResult:
    return StepVerificationResult(
        step_id=2,
        verified=True,
        checks=[
            CriterionCheck(criterion_index=0, satisfied=True, reason="有两个"),
            CriterionCheck(criterion_index=1, satisfied=True, reason="都有名称"),
        ],
    )


def test_valid_step_verification_passes() -> None:
    validate_step_verification(make_step(), make_valid_step_verification())


def test_step_verification_rejects_missing_criterion_index() -> None:
    result = make_valid_step_verification().model_copy(
        update={"checks": [make_valid_step_verification().checks[0]]}
    )

    with pytest.raises(ValueError, match="数量"):
        validate_step_verification(make_step(), result)


def test_step_verification_rejects_duplicate_indexes() -> None:
    result = make_valid_step_verification()
    result.checks[1] = result.checks[1].model_copy(update={"criterion_index": 0})

    with pytest.raises(ValueError, match="必须唯一"):
        validate_step_verification(make_step(), result)


@pytest.mark.parametrize(
    ("verified", "checks"),
    [
        (
            True,
            [
                CriterionCheck(criterion_index=0, satisfied=True, reason="ok"),
                CriterionCheck(criterion_index=1, satisfied=False, reason="missing"),
            ],
        ),
        (
            False,
            [
                CriterionCheck(criterion_index=0, satisfied=True, reason="ok"),
                CriterionCheck(criterion_index=1, satisfied=True, reason="ok"),
            ],
        ),
    ],
)
def test_step_verification_rejects_verified_checks_mismatch(
    verified: bool,
    checks: list[CriterionCheck],
) -> None:
    result = StepVerificationResult(
        step_id=2,
        verified=verified,
        checks=checks,
    )

    with pytest.raises(ValueError, match="必须且仅能"):
        validate_step_verification(make_step(), result)


def test_step_verification_rejects_wrong_step_id() -> None:
    result = make_valid_step_verification().model_copy(update={"step_id": 99})

    with pytest.raises(ValueError, match="step_id"):
        validate_step_verification(make_step(), result)


def test_incomplete_outcome_skips_llm_and_is_rejected() -> None:
    model = Mock(spec=BaseChatModel)
    verifier = TaskVerifier(model=model)

    result = verifier.verify_step(
        task_spec=TaskSpec(goal="整理候选"),
        step=make_step(),
        outcome=StepExecutionOutcome(
            step_id=2,
            claimed_complete=False,
            error="action limit reached",
        ),
        tool_calls=[],
        dependency_results=[],
    )

    assert result.verified is False
    assert len(result.checks) == 2
    assert "action limit reached" in result.feedback
    model.with_structured_output.assert_not_called()


def test_real_step_verifier_uses_structured_output() -> None:
    structured_model = Mock()
    structured_model.invoke.return_value = make_valid_step_verification()
    model = Mock(spec=BaseChatModel)
    model.with_structured_output.return_value = structured_model

    result = TaskVerifier(model=model).verify_step(
        task_spec=TaskSpec(goal="整理候选"),
        step=make_step(),
        outcome=StepExecutionOutcome(
            step_id=2,
            claimed_complete=True,
            summary="完成",
            evidence=["两个候选"],
            output={"items": [{"name": "A"}, {"name": "B"}]},
        ),
        tool_calls=[],
        dependency_results=[StepResult(step_id=1, summary="发现候选")],
    )

    assert result.verified is True
    model.with_structured_output.assert_called_once_with(
        StepVerificationResult,
        method="function_calling",
    )
    structured_model.invoke.assert_called_once()


def test_task_verification_validation_accepts_consistent_results() -> None:
    validate_task_verification(VerificationResult(completed=True, reason="全部满足"))
    validate_task_verification(
        VerificationResult(
            completed=False,
            reason="缺少 CSV",
            missing_requirements=["CSV"],
        )
    )


def test_task_verification_rejects_completed_with_missing_requirements() -> None:
    result = VerificationResult(
        completed=True,
        reason="矛盾输出",
        missing_requirements=["仍缺 CSV"],
    )

    with pytest.raises(ValueError, match="必须为空"):
        validate_task_verification(result)


def test_task_verification_rejects_empty_failure_reason() -> None:
    result = VerificationResult(completed=False, reason="   ")

    with pytest.raises(ValueError, match="reason 不能为空"):
        validate_task_verification(result)


def test_verifier_prompts_preserve_phase_5_boundaries() -> None:
    assert "只验证当前 PlanStep" in STEP_VERIFIER_SYSTEM_PROMPT
    assert "不规划、不执行、不调用工具" in STEP_VERIFIER_SYSTEM_PROMPT
    assert "完成声明本身不是通过依据" in STEP_VERIFIER_SYSTEM_PROMPT
    assert "不得虚构" in STEP_VERIFIER_SYSTEM_PROMPT
    assert "criterion_index" in STEP_VERIFIER_SYSTEM_PROMPT
    assert "feedback" in STEP_VERIFIER_SYSTEM_PROMPT
    assert "所有 PlanStep 已完成并不自动表示 Task 成功" in (TASK_VERIFIER_SYSTEM_PROMPT)
    assert "已经验证的 StepResults" in TASK_VERIFIER_SYSTEM_PROMPT
    assert "不得使用未验证的 Executor claim" in TASK_VERIFIER_SYSTEM_PROMPT
