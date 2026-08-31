"""LangGraph state schema for TaskPilot."""

from typing import Any, TypedDict

from taskpilot.models import (
    PlanStep,
    TaskSpec,
    TaskStatus,
    ToolCallRecord,
    VerificationResult,
)


class TaskState(TypedDict):
    """Structured state passed between TaskPilot graph nodes."""

    task_id: str
    user_input: str
    task_spec: TaskSpec | None
    plan: list[PlanStep]
    current_step_id: int | None
    tool_calls: list[ToolCallRecord]
    task_results: dict[str, Any]
    verification: VerificationResult | None
    status: TaskStatus
    error: str | None


class TaskStateUpdate(TypedDict, total=False):
    """Partial state update returned by a graph node."""

    task_id: str
    user_input: str
    task_spec: TaskSpec | None
    plan: list[PlanStep]
    current_step_id: int | None
    tool_calls: list[ToolCallRecord]
    task_results: dict[str, Any]
    verification: VerificationResult | None
    status: TaskStatus
    error: str | None

