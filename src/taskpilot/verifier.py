"""Step 与 Task 两层完成语义的结构化 Verifier。"""

import json
from typing import Any, Protocol, Sequence

from langchain_core.language_models.chat_models import BaseChatModel

from taskpilot.llm import create_chat_model, invoke_structured_with_retry
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    ToolCallRecord,
    VerificationResult,
)


STEP_VERIFIER_SYSTEM_PROMPT = """你是 Step Verifier，只验证当前 PlanStep，不规划、不执行、不调用工具，也不修改 Task Goal 或 Constraints。
必须对当前 PlanStep 的每一项 success_criteria 分别判断，并使用该条件在原列表中的下标作为 criterion_index。
只能把提供的 StepExecutionOutcome、当前步骤 ToolCallRecords 和 dependency StepResults 当作证据；Executor 的完成声明本身不是通过依据。
不得虚构工具未返回的信息，不得补做任务，也不得使用未验证的其他步骤声明。
checks 必须完整覆盖每一个 success_criteria，criterion_index 不得重复或遗漏。
只有所有 check 的 satisfied 都为 true 时，verified 才能为 true；只要一项不满足，verified 必须为 false。
拒绝时 feedback 必须清楚说明缺少的证据或结果，供同一 Step 的下一次 Executor 尝试使用。
输出必须符合 StepVerificationResult schema。"""


TASK_VERIFIER_SYSTEM_PROMPT = """你是 Task Verifier，只验证整个 Task 是否完成，不规划、不执行、不调用工具，也不修改 Goal、Constraints 或 Completion Criteria。
所有 PlanStep 已完成并不自动表示 Task 成功；必须逐项检查 TaskSpec.completion_criteria。
只能使用提供的、已经验证的 StepResults 和 task_results，不得使用未验证的 Executor claim，也不得虚构缺失信息。
任一 Completion Criterion 不满足时，completed 必须为 false，并在 missing_requirements 中列出缺失要求。
completed 为 true 时 missing_requirements 必须为空；completed 为 false 时 reason 必须清楚说明未完成原因。
next_action 只能给出建议，不得实际重规划或执行。
输出必须符合 VerificationResult schema。"""


class TaskVerifierLike(Protocol):
    """真实 Verifier 与离线 Fake 共同遵循的最小接口。"""

    def verify_step(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        outcome: StepExecutionOutcome,
        tool_calls: Sequence[ToolCallRecord],
        dependency_results: Sequence[StepResult],
    ) -> StepVerificationResult:
        """验证当前步骤的完成声明。"""

        ...

    def verify_task(
        self,
        *,
        task_spec: TaskSpec,
        step_results: Sequence[StepResult],
        task_results: dict[str, Any],
    ) -> VerificationResult:
        """验证整个任务的完成条件。"""

        ...


def validate_step_verification(
    step: PlanStep,
    result: StepVerificationResult,
) -> None:
    """确定性校验 Step Verifier 输出，发现矛盾时直接拒绝。"""

    if result.step_id != step.id:
        raise ValueError(
            "StepVerificationResult.step_id 与当前步骤不一致: "
            f"expected={step.id}, actual={result.step_id}"
        )

    expected_count = len(step.success_criteria)
    if len(result.checks) != expected_count:
        raise ValueError(
            "StepVerificationResult.checks 数量与 success_criteria 不一致: "
            f"expected={expected_count}, actual={len(result.checks)}"
        )

    indexes = [check.criterion_index for check in result.checks]
    if len(indexes) != len(set(indexes)):
        raise ValueError("StepVerificationResult.criterion_index 必须唯一")

    expected_indexes = set(range(expected_count))
    actual_indexes = set(indexes)
    if actual_indexes != expected_indexes:
        raise ValueError(
            "StepVerificationResult.criterion_index 必须完整覆盖 "
            f"0..{expected_count - 1}; actual={sorted(actual_indexes)}"
        )

    all_satisfied = all(check.satisfied for check in result.checks)
    if result.verified != all_satisfied:
        raise ValueError(
            "StepVerificationResult.verified 必须且仅能在全部 checks "
            "satisfied 时为 true"
        )


def validate_task_verification(result: VerificationResult) -> None:
    """确定性校验 Task Verifier 输出的一致性。"""

    if result.completed and result.missing_requirements:
        raise ValueError(
            "VerificationResult.completed 为 true 时 missing_requirements 必须为空"
        )
    if not result.completed and not result.reason.strip():
        raise ValueError("VerificationResult.completed 为 false 时 reason 不能为空")


def incomplete_outcome_rejection(
    step: PlanStep,
    outcome: StepExecutionOutcome,
) -> StepVerificationResult:
    """不调用 LLM，直接拒绝 Executor 尚未完成的声明。"""

    detail = outcome.error or "Executor 未声明当前步骤完成"
    return StepVerificationResult(
        step_id=step.id,
        verified=False,
        checks=[
            CriterionCheck(
                criterion_index=index,
                satisfied=False,
                reason=f"尚无完成该条件的已验证声明：{detail}",
            )
            for index, _ in enumerate(step.success_criteria)
        ],
        feedback=f"Executor 尚未完成当前步骤：{detail}",
    )


class TaskVerifier:
    """使用 Chat Model Structured Output 执行 Step 与 Task 验证。"""

    def __init__(self, model: BaseChatModel | None = None) -> None:
        self._model = model

    def verify_step(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        outcome: StepExecutionOutcome,
        tool_calls: Sequence[ToolCallRecord],
        dependency_results: Sequence[StepResult],
    ) -> StepVerificationResult:
        """使用结构化输出验证当前步骤。"""

        if not outcome.claimed_complete:
            result = incomplete_outcome_rejection(step, outcome)
            validate_step_verification(step, result)
            return result

        relevant_calls = [
            record.model_dump(mode="json")
            for record in tool_calls
            if record.step_id == step.id
        ]
        relevant_dependencies = [
            result.model_dump(mode="json")
            for result in dependency_results
            if result.step_id in step.depends_on
        ]
        context = {
            "task": {
                "goal": task_spec.goal,
                "constraints": task_spec.constraints,
            },
            "current_step": step.model_dump(mode="json"),
            "step_outcome": outcome.model_dump(mode="json"),
            "current_step_tool_calls": relevant_calls,
            "dependency_results": relevant_dependencies,
        }
        result = invoke_structured_with_retry(
            self._get_model(),
            StepVerificationResult,
            [
                ("system", STEP_VERIFIER_SYSTEM_PROMPT),
                ("human", json.dumps(context, ensure_ascii=False, default=str)),
            ],
        )
        validate_step_verification(step, result)
        return result

    def verify_task(
        self,
        *,
        task_spec: TaskSpec,
        step_results: Sequence[StepResult],
        task_results: dict[str, Any],
    ) -> VerificationResult:
        """使用结构化输出验证整个任务。"""

        context = {
            "task": task_spec.model_dump(mode="json"),
            "verified_step_results": [
                result.model_dump(mode="json") for result in step_results
            ],
            "task_results": task_results,
        }
        result = invoke_structured_with_retry(
            self._get_model(),
            VerificationResult,
            [
                ("system", TASK_VERIFIER_SYSTEM_PROMPT),
                ("human", json.dumps(context, ensure_ascii=False, default=str)),
            ],
        )
        validate_task_verification(result)
        return result

    def _get_model(self) -> BaseChatModel:
        """延迟创建模型，保证导入和离线 Fake 测试无需 API Key。"""

        if self._model is None:
            self._model = create_chat_model()
        return self._model
