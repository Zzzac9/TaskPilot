"""单个 PlanStep 的有界 Function Calling Executor Runtime。"""

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from taskpilot.config import redact_sensitive_values
from taskpilot.context import ContextBudgetError, ContextBuilder, ContextSnapshot
from taskpilot.llm import create_chat_model
from taskpilot.models import (
    ExecutorAction,
    ExecutorActionType,
    PlanStep,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    ToolCallRecord,
    ToolCallStatus,
)
from taskpilot.observations import repeated_observation_streak
from taskpilot.policy import (
    ApprovalChoice,
    ApprovalRecord,
    PendingToolAction,
    PolicyDecision,
    PolicyOutcome,
    ToolPolicyLike,
)
from taskpilot.tools.registry import (
    ToolArgumentsError,
    ToolRegistry,
    UnknownToolError,
)


FINISH_STEP_NAME = "finish_step"
MAX_DUPLICATE_OBSERVATION_CORRECTIONS = 2

# 部分兼容模型会忽略 Tool Calling，返回普通文本。
# 仅允许补发一次格式纠偏请求；不能直接将文本视为 finish_step，更不能代替验证。
EMPTY_TOOL_CALL_RETRY_PROMPT = (
    "上一轮没有返回 Tool Call，当前 Executor 不接受普通文本作为动作。"
    "请严格只返回一个合法的 Tool Call：若当前步骤已有真实证据则调用 finish_step，"
    "否则调用一个可用的外部工具继续获取证据。不得虚构工具结果或额外执行任务。"
)
FINISH_STEP_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": FINISH_STEP_NAME,
        "description": (
            "Declare that the current step has sufficient evidence for its "
            "success criteria. This is a claim, not verifier approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Concise summary of what was achieved.",
                },
                "evidence": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Evidence supporting each step success criterion.",
                },
                "output": {
                    "type": "object",
                    "description": (
                        "Structured result produced by this step for verified "
                        "downstream handoff."
                    ),
                },
            },
            "required": ["summary", "evidence", "output"],
            "additionalProperties": False,
        },
    },
}

EXECUTOR_SYSTEM_PROMPT = """你是 Task Executor，只执行当前 PlanStep，不执行未来步骤，也不修改 Task Goal 或 Constraints。
根据当前环境 Observation 决定下一动作；优先使用可用 Tool 获取事实，不得虚构外部结果。
每轮必须且最多选择一个动作：调用一个外部 Tool，或调用内部 finish_step。
Tool 失败后可以根据错误 Observation 调整下一动作，单次 Tool 失败不等于 Step 或 Task 失败。
当 Goal、Constraints 或 success_criteria 要求 exact/verbatim 内容时，传给下游 Tool 的文本必须逐字复用已观察到的原文，不得改写、翻译、重排或替换标点。
所有 Tool arguments 必须直接匹配 JSON Schema；array 字段必须是真实 JSON array，禁止把数组再次 JSON 编码成 string。
浏览器语义定位命中多个同名元素时，应根据已观察顺序设置 locator.index（从 0 开始），不得原样重复失败的歧义定位。
浏览器可能打开新标签页；优先复用 observations/pages 中已经到达目标状态的页面，必要时 switch_page。若当前页面已满足步骤目标，禁止再次 navigate 覆盖它。
一旦 success_criteria 已有证据，应立即调用 finish_step，避免重复 observe 或重复点击耗尽动作额度。
executor_feedback 是控制层对上一动作的纠偏；必须优先遵守并复用已有 observation，不得原样重试被拦截的重复观察。
只有当前 Step 的 success_criteria 已有充分 evidence 时才能调用 finish_step。
finish_step 的 output 必须只包含本步骤实际得到、可交给依赖步骤的结构化结果。
finish_step 只表示 Executor 声称当前 Step 已具备完成条件，最终是否完成由未来 Verifier 判断。
不要直接把 PlanStep 或整个 Task 声称为 completed。"""


@dataclass(frozen=True)
class ExecutorRunResult:
    """TaskExecutor 返回给 Graph 的步骤结果与新增工具记录。"""

    outcome: StepExecutionOutcome | None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    pending_action: PendingToolAction | None = None
    policy_decisions: list[PolicyDecision] = field(default_factory=list)
    fatal_error: bool = False


@dataclass(frozen=True)
class ApprovedActionResult:
    """冻结动作获批后单次执行的记录；不重新询问模型或 Policy。"""

    tool_call: ToolCallRecord | None = None
    fatal_error: bool = False
    error: str | None = None


@dataclass(frozen=True)
class ExecutorDecisionResult:
    """一次动作决策产生的 frozen action 或错误；零 Tool Call 时可纠偏一次。"""

    action: ExecutorAction | None = None
    error: str | None = None
    fatal_error: bool = False
    context_snapshot: ContextSnapshot | None = None
    usage_metadata: dict[str, Any] | None = None
    correction_feedback: str | None = None


class TaskExecutorLike(Protocol):
    """真实 Executor 与离线 Fake 共同遵循的最小接口。"""

    async def execute(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult] = (),
        verification_feedback: StepVerificationResult | None = None,
        policy_decisions: Sequence[PolicyDecision] = (),
        approval_records: Sequence[ApprovalRecord] = (),
    ) -> ExecutorRunResult:
        """只执行传入的当前步骤。"""

        ...

    async def execute_approved_action(
        self,
        *,
        pending_action: PendingToolAction,
        approval_records: Sequence[ApprovalRecord],
    ) -> ApprovedActionResult:
        """只执行已获批的 exact frozen action。"""

        ...


def build_executor_messages(
    *,
    task_spec: TaskSpec,
    step: PlanStep,
    task_results: dict[str, Any],
    tool_calls: Sequence[ToolCallRecord],
    step_results: Sequence[StepResult] = (),
    verification_feedback: StepVerificationResult | None = None,
    policy_decisions: Sequence[PolicyDecision] = (),
    approval_records: Sequence[ApprovalRecord] = (),
    executor_feedback: str | None = None,
    context_builder: ContextBuilder | None = None,
) -> list[BaseMessage]:
    """兼容入口；实际内容统一由 deterministic ContextBuilder 组装。"""

    snapshot = (context_builder or ContextBuilder()).build_executor_context(
        task_spec=task_spec,
        step=step,
        task_results=task_results,
        tool_calls=tool_calls,
        step_results=step_results,
        verification_feedback=verification_feedback,
        policy_decisions=policy_decisions,
        approval_records=approval_records,
        executor_feedback=executor_feedback,
    )
    return [
        SystemMessage(content=EXECUTOR_SYSTEM_PROMPT),
        HumanMessage(
            content=json.dumps(snapshot.content, ensure_ascii=False, default=str)
        ),
    ]


class TaskExecutor:
    """使用原生 Tool Calling 反复执行当前步骤，直到 finish_step 或动作上限。"""

    def __init__(
        self,
        registry: ToolRegistry,
        model: BaseChatModel | None = None,
        max_actions_per_step: int = 10,
        policy: ToolPolicyLike | None = None,
        context_builder: ContextBuilder | None = None,
    ) -> None:
        if max_actions_per_step < 1:
            raise ValueError("max_actions_per_step 必须至少为 1")
        self._registry = registry
        self._model = model
        self._max_actions_per_step = max_actions_per_step
        self._policy = policy
        self._context_builder = context_builder or ContextBuilder()

    @property
    def max_actions_per_step(self) -> int:
        """供 Graph 在模型调用前执行 durable action 上限检查。"""

        return self._max_actions_per_step

    @staticmethod
    def _extract_single_tool_call(
        response: Any, seen_call_ids: set[str]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """统一校验模型的一次 Tool Call；不执行工具、不修改历史状态。

        新版 action-level Graph 与旧版 step-level Executor 共享同一组输入边界，
        避免两条路径分别维护格式检查而产生语义偏差。
        """
        if not isinstance(response, AIMessage):
            return None, "Executor 模型未返回 AIMessage"
        if len(response.tool_calls) != 1:
            return (
                None,
                "Executor 每轮必须返回且只能返回一个 Tool Call; "
                f"实际数量: {len(response.tool_calls)}",
            )
        raw = response.tool_calls[0]
        call_id = raw.get("id")
        if not call_id:
            return None, "Executor Tool Call 缺少 tool_call_id"
        if call_id in seen_call_ids:
            return None, f"Executor Tool Call ID 重复: {call_id}"
        return raw, None

    @staticmethod
    async def _invoke_model_action(
        model_with_tools: Any, messages: Sequence[BaseMessage]
    ) -> Any:
        """只纠正空 Tool Call 响应，至多再调用模型一次且无外部工具副作用。

        不修改原上下文，也不将模型的普通文字插入证据：下一次回复仍必须满足
        _extract_single_tool_call 的格式、身份及后续 Schema/Policy 校验。
        """
        response = await model_with_tools.ainvoke(messages)
        if isinstance(response, AIMessage) and not response.tool_calls:
            response = await model_with_tools.ainvoke(
                [*messages, HumanMessage(content=EMPTY_TOOL_CALL_RETRY_PROMPT)]
            )
        return response

    async def decide_action(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        action_index: int,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult] = (),
        verification_feedback: StepVerificationResult | None = None,
        policy_decisions: Sequence[PolicyDecision] = (),
        approval_records: Sequence[ApprovalRecord] = (),
        executor_feedback: str | None = None,
    ) -> ExecutorDecisionResult:
        """决定并冻结一个动作；遇到空 Tool Call 最多纠偏一次，不执行外部 Tool。"""

        if action_index < 1:
            return ExecutorDecisionResult(
                error="Executor action_index 必须从 1 开始",
                fatal_error=True,
            )
        if not self._registry.list_tools():
            return ExecutorDecisionResult(
                error="Executor 当前没有注册任何外部工具",
                fatal_error=True,
            )
        try:
            snapshot = self._context_builder.build_executor_context(
                task_spec=task_spec,
                step=step,
                task_results=task_results,
                tool_calls=tool_calls,
                step_results=step_results,
                verification_feedback=verification_feedback,
                policy_decisions=policy_decisions,
                approval_records=approval_records,
                executor_feedback=executor_feedback,
            )
        except ContextBudgetError as exc:
            # Context 是 deterministic production boundary；核心超限要返回
            # 可持久化的 Task failure，不能逃出 LangGraph invocation。
            detail = redact_sensitive_values(str(exc))
            return ExecutorDecisionResult(
                error=f"Context budget exceeded: {detail}",
                fatal_error=True,
                context_snapshot=None,
            )
        if self._model is None:
            self._model = create_chat_model()
        schemas = self._registry.list_function_schemas() + [FINISH_STEP_SCHEMA]
        # 明确当前预算，帮助模型优先复用页面证据；不允许因预算不足伪造完成声明。
        budget_hint = (
            f"当前是本步骤第 {action_index}/{self._max_actions_per_step} 次动作决策。"
            "此前执行的所有事实都在 Context 中。若已有充分证据，应立即调用 "
            "finish_step；否则仅执行能带来新信息的动作，避免反复切换或观察已读页面。"
            "即使剩余预算不足，也禁止编造 evidence。"
        )
        messages: list[BaseMessage] = [
            SystemMessage(content=EXECUTOR_SYSTEM_PROMPT + "\n" + budget_hint),
            HumanMessage(
                content=json.dumps(snapshot.content, ensure_ascii=False, default=str)
            ),
        ]
        try:
            response = await self._invoke_model_action(
                self._model.bind_tools(schemas), messages
            )
        except Exception as exc:
            detail = redact_sensitive_values(str(exc))
            return ExecutorDecisionResult(
                error=f"Executor 模型调用失败: {type(exc).__name__}: {detail}",
                fatal_error=True,
                context_snapshot=snapshot,
            )
        # 校验完成前不允许产生执行副作用；旧版循环也调用此校验函数。
        seen_call_ids = (
            {record.call_id for record in tool_calls}
            | {record.call_id for record in approval_records}
            | {decision.call_id for decision in policy_decisions}
        )
        raw, call_error = self._extract_single_tool_call(response, seen_call_ids)
        if call_error is not None:
            return ExecutorDecisionResult(
                error=call_error, fatal_error=True, context_snapshot=snapshot
            )
        assert raw is not None
        call_id = raw["id"]
        name = raw["name"]
        arguments = raw["args"]
        if name == FINISH_STEP_NAME:
            arguments = self._normalize_finish_arguments(arguments)
            error = self._validate_finish_arguments(arguments)
            if error is not None:
                return ExecutorDecisionResult(
                    error=error, fatal_error=True, context_snapshot=snapshot
                )
            return ExecutorDecisionResult(
                action=ExecutorAction(
                    step_id=step.id,
                    action_index=action_index,
                    call_id=call_id,
                    action_type=ExecutorActionType.FINISH_STEP,
                    arguments=arguments,
                ),
                context_snapshot=snapshot,
                usage_metadata=(
                    dict(response.usage_metadata)
                    if response.usage_metadata is not None
                    else None
                ),
            )
        try:
            self._registry.get(name)
            self._registry.validate_arguments(name, arguments)
        except (UnknownToolError, ToolArgumentsError) as exc:
            return ExecutorDecisionResult(
                error=str(exc), fatal_error=True, context_snapshot=snapshot
            )
        if name == "browser_observe":
            streak, page, fingerprint = repeated_observation_streak(
                tool_calls,
                step_id=step.id,
                proposed_arguments=arguments,
            )
            if streak >= 2:
                target = page or "current page"
                return ExecutorDecisionResult(
                    correction_feedback=(
                        "重复观察已被控制层跳过：页面 "
                        f"{target} 已连续 {streak} 次返回相同内容"
                        f"（fingerprint={fingerprint[:12] if fingerprint else 'unknown'}）。"
                        "请直接复用 current_step_observations / observation_reuse；"
                        "若成功条件已满足则 finish_step，否则选择会产生新信息的不同动作。"
                    ),
                    context_snapshot=snapshot,
                    usage_metadata=(
                        dict(response.usage_metadata)
                        if response.usage_metadata is not None
                        else None
                    ),
                )
        return ExecutorDecisionResult(
            action=ExecutorAction(
                step_id=step.id,
                action_index=action_index,
                call_id=call_id,
                action_type=ExecutorActionType.TOOL,
                tool_name=name,
                arguments=arguments,
            ),
            context_snapshot=snapshot,
            usage_metadata=(
                dict(response.usage_metadata)
                if response.usage_metadata is not None
                else None
            ),
        )

    async def assess_action(self, action: ExecutorAction) -> PolicyDecision | None:
        """只评估 frozen Tool action；不执行 Tool。"""

        if action.action_type is not ExecutorActionType.TOOL or not action.tool_name:
            raise ValueError("Risk Policy 只能评估外部 Tool action")
        tool = self._registry.get(action.tool_name)
        self._registry.validate_arguments(action.tool_name, action.arguments)
        if self._policy is None:
            return None
        decision = await self._policy.assess(
            call_id=action.call_id,
            step_id=action.step_id,
            tool=tool,
            arguments=action.arguments,
        )
        if (
            decision.call_id != action.call_id
            or decision.step_id != action.step_id
            or decision.tool_name != action.tool_name
        ):
            raise ValueError("Tool Risk Policy 返回的 action identity 不一致，动作未执行")
        return decision

    def validate_frozen_action(self, action: ExecutorAction) -> None:
        """在 journal 与 invoke 前重新校验 frozen action identity/schema。"""

        if action.action_type is not ExecutorActionType.TOOL or not action.tool_name:
            raise ValueError("execute_tool_action 需要完整 Tool action")
        self._registry.get(action.tool_name)
        self._registry.validate_arguments(action.tool_name, action.arguments)

    def is_replay_safe(self, action: ExecutorAction) -> bool:
        """只采信 Host 已写入 Tool metadata 的显式 replay_safe。"""

        self.validate_frozen_action(action)
        tool = self._registry.get(action.tool_name or "")
        metadata = getattr(tool, "metadata", {}) or {}
        return metadata.get("replay_safe") is True

    async def invoke_frozen_action(self, action: ExecutorAction) -> Any:
        """执行一个已确定的 Tool action；调用方负责 journal ordering。"""

        self.validate_frozen_action(action)
        return await self._registry.invoke(action.tool_name or "", action.arguments)

    async def execute(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult] = (),
        verification_feedback: StepVerificationResult | None = None,
        policy_decisions: Sequence[PolicyDecision] = (),
        approval_records: Sequence[ApprovalRecord] = (),
    ) -> ExecutorRunResult:
        """运行当前 Step 的单动作循环，不直接修改 PlanStep 状态。"""

        if not self._registry.list_tools():
            return self._error_result(
                step_id=step.id,
                action_count=0,
                error="Executor 当前没有注册任何外部工具",
                fatal_error=True,
            )

        if self._model is None:
            self._model = create_chat_model()
        schemas = self._registry.list_function_schemas() + [FINISH_STEP_SCHEMA]
        model_with_tools = self._model.bind_tools(schemas)
        messages = build_executor_messages(
            task_spec=task_spec,
            step=step,
            task_results=task_results,
            tool_calls=tool_calls,
            step_results=step_results,
            verification_feedback=verification_feedback,
            policy_decisions=policy_decisions,
            approval_records=approval_records,
            context_builder=self._context_builder,
        )
        new_records: list[ToolCallRecord] = []
        new_decisions: list[PolicyDecision] = []
        duplicate_observation_corrections = 0
        seen_call_ids = (
            {record.call_id for record in tool_calls}
            | {record.call_id for record in approval_records}
            | {decision.call_id for decision in policy_decisions}
        )

        for action_index in range(self._max_actions_per_step):
            action_count = action_index + 1
            try:
                response = await self._invoke_model_action(model_with_tools, messages)
            except Exception as exc:
                error_detail = redact_sensitive_values(str(exc))
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=(
                        "Executor 模型调用失败: "
                        f"{type(exc).__name__}: {error_detail}"
                    ),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )
            # 与 durable Graph 共用同一校验，但旧循环仍由自己维护 seen ID 集合。
            action, call_error = self._extract_single_tool_call(
                response, seen_call_ids
            )
            if call_error is not None:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=call_error,
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )
            assert action is not None
            action_name = action["name"]
            arguments = action["args"]
            call_id = action["id"]
            seen_call_ids.add(call_id)

            if action_name == FINISH_STEP_NAME:
                arguments = self._normalize_finish_arguments(arguments)
                finish_error = self._validate_finish_arguments(arguments)
                if finish_error is not None:
                    return self._error_result(
                        step_id=step.id,
                        action_count=action_count,
                        error=finish_error,
                        tool_calls=new_records,
                        policy_decisions=new_decisions,
                        fatal_error=True,
                    )
                return ExecutorRunResult(
                    outcome=StepExecutionOutcome(
                        step_id=step.id,
                        claimed_complete=True,
                        summary=arguments["summary"],
                        evidence=arguments["evidence"],
                        output=arguments["output"],
                        action_count=action_count,
                    ),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                )

            try:
                tool = self._registry.get(action_name)
            except UnknownToolError as exc:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=str(exc),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )

            try:
                self._registry.validate_arguments(action_name, arguments)
            except ToolArgumentsError as exc:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=str(exc),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )

            if action_name == "browser_observe":
                streak, page, fingerprint = repeated_observation_streak(
                    [*tool_calls, *new_records],
                    step_id=step.id,
                    proposed_arguments=arguments,
                )
                if streak >= 2:
                    duplicate_observation_corrections += 1
                    feedback = (
                        "重复观察已被控制层跳过：页面 "
                        f"{page or 'current page'} 已连续 {streak} 次返回相同内容"
                        f"（fingerprint={fingerprint[:12] if fingerprint else 'unknown'}）。"
                        "请复用已有结果，完成条件满足时调用 finish_step，"
                        "否则选择会产生新信息的不同动作。"
                    )
                    if (
                        duplicate_observation_corrections
                        > MAX_DUPLICATE_OBSERVATION_CORRECTIONS
                    ):
                        return self._error_result(
                            step_id=step.id,
                            action_count=action_count,
                            error=(
                                "Executor duplicate observation correction exhausted "
                                f"after {MAX_DUPLICATE_OBSERVATION_CORRECTIONS} retries"
                            ),
                            tool_calls=new_records,
                            policy_decisions=new_decisions,
                            fatal_error=True,
                        )
                    messages.append(response)
                    messages.append(
                        ToolMessage(
                            content=json.dumps(
                                {"status": "skipped", "feedback": feedback},
                                ensure_ascii=False,
                            ),
                            tool_call_id=call_id,
                            name=action_name,
                            status="error",
                        )
                    )
                    continue
            else:
                duplicate_observation_corrections = 0

            if self._policy is not None:
                try:
                    decision = await self._policy.assess(
                        call_id=call_id,
                        step_id=step.id,
                        tool=tool,
                        arguments=arguments,
                    )
                except Exception as exc:
                    error_detail = redact_sensitive_values(str(exc))
                    return self._error_result(
                        step_id=step.id,
                        action_count=action_count,
                        error=(
                            "Tool Risk Policy 评估失败，动作未执行: "
                            f"{type(exc).__name__}: {error_detail}"
                        ),
                        tool_calls=new_records,
                        policy_decisions=new_decisions,
                        fatal_error=True,
                    )
                if (
                    decision.call_id != call_id
                    or decision.step_id != step.id
                    or decision.tool_name != action_name
                ):
                    return self._error_result(
                        step_id=step.id,
                        action_count=action_count,
                        error="Tool Risk Policy 返回的 action identity 不一致，动作未执行",
                        tool_calls=new_records,
                        policy_decisions=new_decisions,
                        fatal_error=True,
                    )
                new_decisions.append(decision)
                if decision.outcome is PolicyOutcome.REQUIRE_APPROVAL:
                    return ExecutorRunResult(
                        outcome=None,
                        tool_calls=new_records,
                        pending_action=PendingToolAction(
                            call_id=call_id,
                            step_id=step.id,
                            tool_name=action_name,
                            arguments=dict(arguments),
                            policy_decision=decision,
                        ),
                        policy_decisions=new_decisions,
                    )
                if decision.outcome is PolicyOutcome.DENY:
                    messages.append(response)
                    messages.append(
                        ToolMessage(
                            content=json.dumps(
                                {
                                    "status": "denied",
                                    "policy_decision": decision.model_dump(
                                        mode="json"
                                    ),
                                },
                                ensure_ascii=False,
                            ),
                            tool_call_id=call_id,
                            name=action_name,
                            status="error",
                        )
                    )
                    continue

            try:
                result = await self._registry.invoke(action_name, arguments)
            except ToolArgumentsError as exc:  # pragma: no cover - 已预校验
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=str(exc),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )
            except Exception as exc:
                error = redact_sensitive_values(str(exc))
                record = ToolCallRecord(
                    call_id=call_id,
                    step_id=step.id,
                    tool_name=action_name,
                    arguments=arguments,
                    status=ToolCallStatus.FAILED,
                    error=f"{type(exc).__name__}: {error}",
                )
                observation = {
                    "status": "failed",
                    "error": record.error,
                }
                tool_message_status = "error"
            else:
                record = ToolCallRecord(
                    call_id=call_id,
                    step_id=step.id,
                    tool_name=action_name,
                    arguments=arguments,
                    status=ToolCallStatus.SUCCESS,
                    result=result,
                )
                observation = {"status": "success", "result": result}
                tool_message_status = "success"

            new_records.append(record)
            messages.append(response)
            messages.append(
                ToolMessage(
                    content=json.dumps(observation, ensure_ascii=False, default=str),
                    tool_call_id=call_id,
                    name=action_name,
                    status=tool_message_status,
                )
            )

        return self._error_result(
            step_id=step.id,
            action_count=self._max_actions_per_step,
            error=(
                "Executor action limit reached: "
                f"{self._max_actions_per_step}"
            ),
            tool_calls=new_records,
            policy_decisions=new_decisions,
            fatal_error=False,
        )

    async def execute_approved_action(
        self,
        *,
        pending_action: PendingToolAction,
        approval_records: Sequence[ApprovalRecord],
    ) -> ApprovedActionResult:
        """执行一次冻结动作；不重新调用 LLM 或 Risk Policy。"""

        matching = [
            record
            for record in approval_records
            if record.call_id == pending_action.call_id
            and record.step_id == pending_action.step_id
            and record.tool_name == pending_action.tool_name
        ]
        if len(matching) != 1 or matching[0].decision is not ApprovalChoice.APPROVE:
            return ApprovedActionResult(
                fatal_error=True,
                error="冻结 Tool action 缺少唯一 APPROVE record",
            )
        try:
            self._registry.get(pending_action.tool_name)
            self._registry.validate_arguments(
                pending_action.tool_name,
                pending_action.arguments,
            )
        except (UnknownToolError, ToolArgumentsError) as exc:
            return ApprovedActionResult(fatal_error=True, error=str(exc))

        try:
            result = await self._registry.invoke(
                pending_action.tool_name,
                pending_action.arguments,
            )
        except Exception as exc:
            detail = redact_sensitive_values(str(exc))
            record = ToolCallRecord(
                call_id=pending_action.call_id,
                step_id=pending_action.step_id,
                tool_name=pending_action.tool_name,
                arguments=pending_action.arguments,
                status=ToolCallStatus.FAILED,
                error=f"{type(exc).__name__}: {detail}",
            )
        else:
            record = ToolCallRecord(
                call_id=pending_action.call_id,
                step_id=pending_action.step_id,
                tool_name=pending_action.tool_name,
                arguments=pending_action.arguments,
                status=ToolCallStatus.SUCCESS,
                result=result,
            )
        return ApprovedActionResult(tool_call=record)

    @staticmethod
    def _normalize_finish_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        """Normalize provider-added string layers around finish evidence."""

        normalized = dict(arguments)
        evidence = normalized.get("evidence")
        if not isinstance(evidence, str):
            return normalized
        try:
            decoded = json.loads(evidence)
        except json.JSONDecodeError:
            if evidence.strip():
                normalized["evidence"] = [evidence.strip()]
            return normalized
        if isinstance(decoded, list) and all(
            isinstance(item, str) for item in decoded
        ):
            normalized["evidence"] = decoded
        elif isinstance(decoded, str) and decoded.strip():
            normalized["evidence"] = [decoded.strip()]
        return normalized

    @staticmethod
    def _validate_finish_arguments(arguments: dict[str, Any]) -> str | None:
        parameters = FINISH_STEP_SCHEMA["function"]["parameters"]
        try:
            Draft202012Validator(parameters).validate(arguments)
        except ValidationError as exc:
            return f"finish_step 参数无效: {exc.message}"
        return None

    @staticmethod
    def _error_result(
        *,
        step_id: int,
        action_count: int,
        error: str,
        tool_calls: list[ToolCallRecord] | None = None,
        policy_decisions: list[PolicyDecision] | None = None,
        fatal_error: bool,
    ) -> ExecutorRunResult:
        return ExecutorRunResult(
            outcome=StepExecutionOutcome(
                step_id=step_id,
                claimed_complete=False,
                action_count=action_count,
                error=redact_sensitive_values(error),
            ),
            tool_calls=tool_calls or [],
            policy_decisions=policy_decisions or [],
            fatal_error=fatal_error,
        )
