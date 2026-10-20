"""TaskPilot 任务状态使用的结构化数据模型。"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PlanStepStatus(str, Enum):
    """单个计划步骤的生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ToolCallStatus(str, Enum):
    """一次工具调用的生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class TaskStatus(str, Enum):
    """整个任务的生命周期状态。"""

    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_HUMAN_ASSISTANCE = "waiting_human_assistance"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ExecutorActionType(str, Enum):
    """Executor 单次模型决策的动作类型。"""

    TOOL = "tool"
    FINISH_STEP = "finish_step"


class ReplanTrigger(str, Enum):
    """触发动态重规划的任务语义。"""

    STEP_ATTEMPTS_EXHAUSTED = "step_attempts_exhausted"
    TASK_VERIFICATION_FAILED = "task_verification_failed"
    EXECUTOR_BLOCKED = "executor_blocked"


class ReplanDecision(str, Enum):
    """Replanner 只能建议；最终路由仍由 Runtime 校验。"""

    REPLAN = "REPLAN"
    REQUEST_HUMAN = "REQUEST_HUMAN"
    ABORT = "ABORT"


class ExecutionBlockerCategory(str, Enum):
    """Runtime 可审计的不可自动恢复阻塞类别。"""

    LOGIN_REQUIRED = "login_required"
    CAPTCHA_REQUIRED = "captcha_required"
    PERMISSION_DENIED = "permission_denied"


class HumanAssistanceChoice(str, Enum):
    """Human Assistance 与审批授权刻意使用不同词汇。"""

    COMPLETED = "completed"
    CANCEL = "cancel"


class TaskSpec(BaseModel):
    """对用户任务进行规范化描述。"""

    goal: str
    # 每个模型实例都创建独立容器，避免可变默认值在实例之间共享。
    constraints: dict[str, Any] = Field(default_factory=dict)
    expected_output: str | None = None
    completion_criteria: list[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    """任务计划中的一个可执行步骤。"""

    id: int
    description: str
    status: PlanStepStatus = PlanStepStatus.PENDING
    retry_count: int = 0
    depends_on: list[int] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    # revision 由 Graph 写入，模型输出的值不作为可信控制信息。
    revision: int = 0


class TaskPlan(BaseModel):
    """Initial Planner 生成的结构化任务计划。"""

    steps: list[PlanStep]


class ReplanRequest(BaseModel):
    """Graph 根据真实失败事实创建的重规划请求。"""

    trigger: ReplanTrigger
    failed_step_id: int | None = None
    reason: str
    missing_requirements: list[str] = Field(default_factory=list)
    verification_feedback: str | None = None
    blocking_failure: "ExecutionBlocker | None" = None


class ReplanProposal(BaseModel):
    """Replanner 的结构化建议；Runtime 决定是否接受以及如何路由。"""

    decision: ReplanDecision = ReplanDecision.REPLAN
    reason: str
    steps: list[PlanStep] = Field(default_factory=list)
    required_human_action: str | None = None
    resume_condition: str | None = None


class ExecutionBlocker(BaseModel):
    """Executor Tool 失败后由 Runtime 分类出的结构化阻塞事实。"""

    category: ExecutionBlockerCategory
    step_id: int
    call_id: str
    tool_name: str
    reason: str
    retryable: bool = False
    requires_browser: bool = False


class HumanAssistanceRequest(BaseModel):
    """已通过 Runtime 校验、可进入 durable interrupt 的协助请求。"""

    request_id: str
    step_id: int
    reason: str
    required_action: str
    resume_condition: str
    blocker: ExecutionBlocker


class HumanAssistanceResponse(BaseModel):
    """严格限制 resume value，绝不承载审批或冻结动作修改。"""

    model_config = ConfigDict(extra="forbid")

    choice: HumanAssistanceChoice
    reason: str | None = None


class ReplanRecord(BaseModel):
    """一次已接受重规划的持久化摘要。"""

    revision: int
    trigger: ReplanTrigger
    reason: str
    decision: ReplanDecision = ReplanDecision.REPLAN
    replaced_step_ids: list[int] = Field(default_factory=list)
    new_step_ids: list[int] = Field(default_factory=list)


class StepExecutionOutcome(BaseModel):
    """Executor 对当前步骤执行结果的结构化声明。"""

    step_id: int
    claimed_complete: bool = False
    summary: str | None = None
    evidence: list[str] = Field(default_factory=list)
    # output 在 Verifier 接受前仍是不可信的 Executor 声明。
    output: dict[str, Any] = Field(default_factory=dict)
    action_count: int = 0
    error: str | None = None


class StepResult(BaseModel):
    """已经通过 Step Verifier 的正式步骤产物。"""

    step_id: int
    summary: str
    output: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)


class CriterionCheck(BaseModel):
    """对一个步骤成功条件的独立验证结论。"""

    criterion_index: int
    satisfied: bool
    reason: str


class StepVerificationResult(BaseModel):
    """Step Verifier 对当前步骤完成声明的验证结果。"""

    step_id: int
    verified: bool
    checks: list[CriterionCheck] = Field(default_factory=list)
    feedback: str | None = None


class ToolCallRecord(BaseModel):
    """一次工具调用的原始执行记录。"""

    call_id: str
    step_id: int | None = None
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: ToolCallStatus = ToolCallStatus.PENDING
    result: Any | None = None
    error: str | None = None


class ExecutorAction(BaseModel):
    """已冻结并可写入 checkpoint 的单个 Executor 动作。"""

    step_id: int
    action_index: int
    call_id: str
    action_type: ExecutorActionType
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(BaseModel):
    """任务完成条件的校验结果。"""

    completed: bool
    reason: str
    missing_requirements: list[str] = Field(default_factory=list)
    next_action: str | None = None
