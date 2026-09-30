"""风险判断、待审批动作与人工决定的数据模型。"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RiskLevel(str, Enum):
    """Tool action 的确定性风险等级。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PolicyOutcome(str, Enum):
    """Policy 对动作执行边界的决定。"""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class PolicyDecision(BaseModel):
    """Policy 对一次模型 Tool Call 的 JSON-serializable 判断。"""

    call_id: str
    step_id: int
    tool_name: str
    risk_level: RiskLevel
    outcome: PolicyOutcome
    reason: str
    tool_source: str
    preview: dict[str, Any] = Field(default_factory=dict)
    annotations_trusted: bool | None = None


class PendingToolAction(BaseModel):
    """已经请求但尚未执行、等待 Human Approval 的冻结动作。"""

    call_id: str
    step_id: int
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    policy_decision: PolicyDecision


class ApprovalChoice(str, Enum):
    """Human 对冻结动作仅能批准或拒绝。"""

    APPROVE = "approve"
    REJECT = "reject"


class ApprovalResponse(BaseModel):
    """LangGraph resume value；禁止夹带修改后的 Tool arguments。"""

    model_config = ConfigDict(extra="forbid")

    decision: ApprovalChoice
    reason: str | None = None


class ApprovalRecord(BaseModel):
    """一次 Human Approval 的不可变语义记录。"""

    call_id: str
    step_id: int
    tool_name: str
    decision: ApprovalChoice
    reason: str | None = None
