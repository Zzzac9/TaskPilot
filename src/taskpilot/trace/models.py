"""Structured trace 的 JSON-serializable 事件模型。"""

from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class TraceEventType(str, Enum):
    """Phase 10 需要审计和后续 Benchmark 聚合的稳定事件类型。"""

    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_ANALYZED = "task_analyzed"
    PLAN_CREATED = "plan_created"
    PLAN_REPLANNED = "plan_replanned"
    STEP_STARTED = "step_started"
    STEP_VERIFIED = "step_verified"
    STEP_REJECTED = "step_rejected"
    CONTEXT_BUILT = "context_built"
    LLM_ACTION_DECIDED = "llm_action_decided"
    POLICY_DECIDED = "policy_decided"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    TOOL_STARTED = "tool_started"
    TOOL_SUCCEEDED = "tool_succeeded"
    TOOL_FAILED = "tool_failed"
    TOOL_MATERIALIZED_FROM_JOURNAL = "tool_materialized_from_journal"
    VISION_FALLBACK_REQUESTED = "vision_fallback_requested"
    VISION_TARGET_FOUND = "vision_target_found"
    VISION_TARGET_REJECTED = "vision_target_rejected"
    RECOVERY_REQUIRED = "recovery_required"
    RECOVERY_RESOLVED = "recovery_resolved"
    TASK_VERIFIED = "task_verified"
    TASK_VERIFICATION_REJECTED = "task_verification_rejected"


class TraceEvent(BaseModel):
    """一次独立提交的审计事件；不进入 LangGraph State。"""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    event_type: TraceEventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    step_id: int | None = None
    call_id: str | None = None
    plan_revision: int = 0
    status: str | None = None
    latency_ms: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class TraceSummary(BaseModel):
    """Phase 12 可直接复用的确定性任务 Trace 聚合。"""

    event_count: int
    tool_call_count: int
    tool_failure_count: int
    step_rejection_count: int
    replan_count: int
    approval_request_count: int
    recovery_required_count: int
    observed_latency_ms: float
