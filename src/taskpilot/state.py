"""TaskPilot 使用的 LangGraph 状态结构。"""

from typing import Any, TypedDict

from taskpilot.models import (
    ExecutorAction,
    PlanStep,
    ReplanRecord,
    ReplanRequest,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    TaskStatus,
    ToolCallRecord,
    VerificationResult,
)
from taskpilot.persistence.models import RecoveryIssue
from taskpilot.policy.models import ApprovalRecord, PendingToolAction, PolicyDecision


class TaskState(TypedDict):
    """在 TaskPilot 图节点之间传递的结构化任务状态。"""

    task_id: str
    user_input: str
    task_spec: TaskSpec | None
    plan: list[PlanStep]
    current_step_id: int | None
    step_outcome: StepExecutionOutcome | None
    # step_results 只保存通过 Step Verifier 的正式步骤产物。
    step_results: list[StepResult]
    # step_verification 是当前步骤最近一次验证，不等同于任务级 verification。
    step_verification: StepVerificationResult | None
    # tool_calls 保留原始执行记录，便于后续追踪与恢复。
    tool_calls: list[ToolCallRecord]
    # task_results 只保存与最终目标直接相关的提炼结果，不与工具日志混用。
    task_results: dict[str, Any]
    # final_answer 是 Task Verifier 通过后由独立模型生成并持久化的用户答复。
    final_answer: str | None
    verification: VerificationResult | None
    # pending_action 是未执行的冻结动作，不属于 ToolCallRecord。
    pending_action: PendingToolAction | None
    policy_decisions: list[PolicyDecision]
    approval_records: list[ApprovalRecord]
    # 每次模型 decision 后递增；审批与崩溃恢复都不能偷偷重置。
    current_action_index: int
    # 模型 decision 与真实 Tool execution 之间的 durable frozen action。
    pending_executor_action: ExecutorAction | None
    # STARTED 且不可安全重放时保存给人工恢复审查的问题。
    recovery_issue: RecoveryIssue | None
    # Replan 控制事实属于 durable State；模型 Context 与 Trace 均不放在这里。
    pending_replan: ReplanRequest | None
    plan_revision: int
    replan_count: int
    replan_history: list[ReplanRecord]
    # Host 运行能力开关随 checkpoint 保存；不包含 Browser Page 等 live object。
    browser_enabled: bool
    status: TaskStatus
    error: str | None


class TaskStateUpdate(TypedDict, total=False):
    """图节点返回的部分状态更新；未返回的字段由 LangGraph 原样保留。"""

    task_id: str
    user_input: str
    task_spec: TaskSpec | None
    plan: list[PlanStep]
    current_step_id: int | None
    step_outcome: StepExecutionOutcome | None
    step_results: list[StepResult]
    step_verification: StepVerificationResult | None
    tool_calls: list[ToolCallRecord]
    task_results: dict[str, Any]
    final_answer: str | None
    verification: VerificationResult | None
    pending_action: PendingToolAction | None
    policy_decisions: list[PolicyDecision]
    approval_records: list[ApprovalRecord]
    current_action_index: int
    pending_executor_action: ExecutorAction | None
    recovery_issue: RecoveryIssue | None
    pending_replan: ReplanRequest | None
    plan_revision: int
    replan_count: int
    replan_history: list[ReplanRecord]
    browser_enabled: bool
    status: TaskStatus
    error: str | None
