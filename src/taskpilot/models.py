"""TaskPilot 任务状态使用的结构化数据模型。"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


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
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ExecutorActionType(str, Enum):
    """Executor 单次模型决策的动作类型。"""

    TOOL = "tool"
    FINISH_STEP = "finish_step"


class ReplanTrigger(str, Enum):
    """触发动态重规划的两种任务语义。"""

    STEP_ATTEMPTS_EXHAUSTED = "step_attempts_exhausted"
    TASK_VERIFICATION_FAILED = "task_verification_failed"


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


class ReplanProposal(BaseModel):
    """Replanner 只提出后续语义步骤，不复制历史计划。"""

    reason: str
    steps: list[PlanStep]


class ReplanRecord(BaseModel):
    """一次已接受重规划的持久化摘要。"""

    revision: int
    trigger: ReplanTrigger
    reason: str
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
