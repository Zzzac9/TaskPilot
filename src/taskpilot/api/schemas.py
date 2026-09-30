"""HTTP request/response 的显式白名单 schema。"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictSchema(BaseModel):
    """拒绝未声明字段，尤其不接受 executable/command/Tool arguments。"""

    model_config = ConfigDict(extra="forbid")


class CreateTaskRequest(StrictSchema):
    task: str = Field(min_length=1)
    enable_browser: bool = False


class ToolApprovalResumeRequest(StrictSchema):
    type: Literal["tool_approval"]
    decision: Literal["approve", "reject"]
    reason: str | None = None


class RecoveryResumeRequest(StrictSchema):
    type: Literal["ambiguous_tool_recovery"]
    choice: Literal["retry", "abort"]
    reason: str | None = None


ResumeTaskRequest = Annotated[
    ToolApprovalResumeRequest | RecoveryResumeRequest,
    Field(discriminator="type"),
]


class PlanStepView(StrictSchema):
    id: int
    description: str
    status: str
    depends_on: list[int]
    success_criteria: list[str]


class InterruptView(StrictSchema):
    type: str
    tool_name: str | None = None
    risk_level: str | None = None
    reason: str | None = None
    arguments_preview: dict[str, Any] | None = None
    choices: list[str] | None = None


class TaskView(StrictSchema):
    task_id: str
    status: str
    goal: str | None
    current_step_id: int | None
    plan_revision: int
    replan_count: int
    plan: list[PlanStepView]
    interrupt: InterruptView | None
    final_answer: str | None
    error: str | None


class TraceSummaryView(StrictSchema):
    event_count: int
    tool_call_count: int
    tool_failure_count: int
    step_rejection_count: int
    replan_count: int
    approval_request_count: int
    recovery_required_count: int
    observed_latency_ms: float
