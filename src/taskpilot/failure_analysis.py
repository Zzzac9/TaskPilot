"""失败原因分类只引用已经发生的 Runtime / Verifier 事实，不重新解释目标。"""

from enum import Enum

from pydantic import BaseModel

from taskpilot.models import StepExecutionOutcome, StepVerificationResult


class FailureCategory(str, Enum):
    ACTION_LIMIT = "action_limit"
    GLOBAL_BUDGET = "global_budget"
    TOOL_ERROR = "tool_error"
    EVIDENCE_GAP = "evidence_gap"
    VERIFICATION_REJECTED = "verification_rejected"


class FailureDiagnosis(BaseModel):
    category: FailureCategory
    can_replan: bool
    detail: str


def diagnose_step_failure(
    outcome: StepExecutionOutcome, verification: StepVerificationResult
) -> FailureDiagnosis:
    """分类不是成功证据；不能单靠报错文字认定非必要要求已满足。"""
    error = outcome.error or ""
    if error.startswith("Task action budget reached:"):
        return FailureDiagnosis(category=FailureCategory.GLOBAL_BUDGET,
                                can_replan=False, detail=error)
    if error.startswith("Executor action limit reached:"):
        return FailureDiagnosis(category=FailureCategory.ACTION_LIMIT,
                                can_replan=True, detail=error)
    if error:
        return FailureDiagnosis(category=FailureCategory.TOOL_ERROR,
                                can_replan=True, detail=error)
    if outcome.claimed_complete and not verification.verified:
        return FailureDiagnosis(category=FailureCategory.EVIDENCE_GAP,
                                can_replan=True,
                                detail=verification.feedback or "完成声明缺少足够证据")
    return FailureDiagnosis(category=FailureCategory.VERIFICATION_REJECTED,
                            can_replan=True,
                            detail=verification.feedback or "步骤尚未通过验证")
