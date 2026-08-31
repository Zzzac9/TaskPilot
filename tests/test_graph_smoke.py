"""Offline smoke test for the Phase 1 graph."""

from taskpilot.graph import build_graph
from taskpilot.models import TaskStatus
from taskpilot.state import TaskState


def test_graph_initializes_task() -> None:
    initial_state: TaskState = {
        "task_id": "task-001",
        "user_input": "Find three gyms and generate a CSV",
        "task_spec": None,
        "plan": [],
        "current_step_id": None,
        "tool_calls": [],
        "task_results": {},
        "verification": None,
        "status": TaskStatus.CREATED,
        "error": None,
    }

    result = build_graph().invoke(initial_state)

    assert result["task_id"] == initial_state["task_id"]
    assert result["user_input"] == initial_state["user_input"]
    assert result["status"] is TaskStatus.RUNNING

