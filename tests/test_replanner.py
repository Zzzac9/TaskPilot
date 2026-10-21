"""Phase 10 Replanner prompt 与 deterministic validation 测试。"""

from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel

from taskpilot.context import ContextSnapshot
from taskpilot.models import (
    PlanStep,
    PlanStepStatus,
    ReplanProposal,
    ReplanDecision,
    ReplanRecord,
    ReplanRequest,
    ReplanTrigger,
)
from taskpilot.replanner import (
    REPLANNER_SYSTEM_PROMPT,
    TaskReplanner,
    validate_replan_proposal,
)


def historical(status: PlanStepStatus = PlanStepStatus.COMPLETED) -> list[PlanStep]:
    return [
        PlanStep(
            id=1,
            description="old",
            status=status,
            success_criteria=["old done"],
        )
    ]


def proposal(*steps: PlanStep) -> ReplanProposal:
    return ReplanProposal(reason="new path", steps=list(steps))


def new_step(step_id: int = 2, **updates: object) -> PlanStep:
    values = {
        "id": step_id,
        "description": "new semantic goal",
        "success_criteria": ["new done"],
    }
    values.update(updates)
    return PlanStep(**values)


def test_replanner_prompt_enforces_plan_only_boundary() -> None:
    for phrase in [
        "WHAT",
        "不得选择或调用 Tool",
        "Constraints 不允许修改",
        "不得虚构",
        "REQUEST_HUMAN",
        "ABORT",
    ]:
        assert phrase in REPLANNER_SYSTEM_PROMPT


@pytest.mark.parametrize(
    "decision",
    [ReplanDecision.REPLAN, ReplanDecision.REQUEST_HUMAN, ReplanDecision.ABORT],
)
def test_deterministic_fake_llm_covers_all_replanner_decisions(
    decision: ReplanDecision,
) -> None:
    expected = ReplanProposal(
        decision=decision,
        reason="fixture",
        steps=[new_step()] if decision is ReplanDecision.REPLAN else [],
        required_human_action=(
            "sign in" if decision is ReplanDecision.REQUEST_HUMAN else None
        ),
        resume_condition=(
            "authenticated page is observable"
            if decision is ReplanDecision.REQUEST_HUMAN
            else None
        ),
    )
    structured = MagicMock()
    structured.invoke.return_value = expected
    model = MagicMock(spec=BaseChatModel)
    model.with_structured_output.return_value = structured

    actual = TaskReplanner(model).replan(
        ContextSnapshot(content={"fixture": True}, total_chars=16)
    )

    assert actual == expected
    model.with_structured_output.assert_called_once_with(
        ReplanProposal,
        method="function_calling",
    )


def test_valid_replan_accepts_completed_historical_dependency() -> None:
    validate_replan_proposal(proposal(new_step(depends_on=[1])), historical())


def test_valid_replan_accepts_earlier_new_dependency() -> None:
    validate_replan_proposal(
        proposal(new_step(2), new_step(3, depends_on=[2])), historical()
    )


def test_empty_replan_is_rejected() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        validate_replan_proposal(proposal(), historical())


def test_duplicate_new_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="唯一"):
        validate_replan_proposal(proposal(new_step(), new_step()), historical())


@pytest.mark.parametrize("step_id", [0, 1])
def test_reused_or_old_step_ids_are_rejected(step_id: int) -> None:
    with pytest.raises(ValueError, match="historical_max_step_id"):
        validate_replan_proposal(proposal(new_step(step_id)), historical())


def test_empty_success_criteria_is_rejected() -> None:
    with pytest.raises(ValueError, match="success_criteria"):
        validate_replan_proposal(proposal(new_step(success_criteria=[])), historical())


def test_self_dependency_is_rejected() -> None:
    with pytest.raises(ValueError, match="依赖自身"):
        validate_replan_proposal(proposal(new_step(depends_on=[2])), historical())


def test_nonexistent_dependency_is_rejected() -> None:
    with pytest.raises(ValueError, match="不存在"):
        validate_replan_proposal(proposal(new_step(depends_on=[99])), historical())


@pytest.mark.parametrize(
    "status",
    [
        PlanStepStatus.FAILED,
        PlanStepStatus.SKIPPED,
        PlanStepStatus.PENDING,
        PlanStepStatus.RUNNING,
    ],
)
def test_noncompleted_historical_dependency_is_rejected(status: PlanStepStatus) -> None:
    with pytest.raises(ValueError, match=status.value):
        validate_replan_proposal(proposal(new_step(depends_on=[1])), historical(status))


def test_later_new_dependency_is_rejected() -> None:
    with pytest.raises(ValueError, match="必须更早出现"):
        validate_replan_proposal(
            proposal(new_step(2, depends_on=[3]), new_step(3)), historical()
        )


def test_nonpending_status_is_rejected() -> None:
    with pytest.raises(ValueError, match="status"):
        validate_replan_proposal(
            proposal(new_step(status=PlanStepStatus.RUNNING)), historical()
        )


def test_nonzero_retry_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="retry_count"):
        validate_replan_proposal(proposal(new_step(retry_count=1)), historical())


def test_replan_model_mutable_defaults_are_isolated() -> None:
    first = ReplanRequest(trigger=ReplanTrigger.TASK_VERIFICATION_FAILED, reason="one")
    second = ReplanRequest(trigger=ReplanTrigger.TASK_VERIFICATION_FAILED, reason="two")
    first.missing_requirements.append("x")
    assert second.missing_requirements == []
    record_a = ReplanRecord(revision=1, trigger=first.trigger, reason="a")
    record_b = ReplanRecord(revision=1, trigger=first.trigger, reason="b")
    record_a.new_step_ids.append(4)
    assert record_b.new_step_ids == []



def test_replan_cannot_create_untraceable_requirement_or_lose_required_goal() -> None:
    from taskpilot.models import TaskSpec

    spec = TaskSpec(
        goal="Return two real results",
        completion_criteria=["two entries", "grounded citations"],
        optional_criteria=["independent detail link if available"],
    )
    old = historical(PlanStepStatus.FAILED)
    with pytest.raises(ValueError, match="Requirement ID"):
        validate_replan_proposal(
            proposal(new_step(requirement_ids=["R999"])), old, spec
        )
    with pytest.raises(ValueError, match="丢失用户必需条件"):
        validate_replan_proposal(
            proposal(new_step(requirement_ids=["R1", "O1"])), old, spec
        )
    validate_replan_proposal(
        proposal(new_step(requirement_ids=["R1", "R2"])), old, spec
    )
