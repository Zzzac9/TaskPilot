"""Minimal LangGraph workflow for TaskPilot Phase 1."""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from taskpilot.models import TaskStatus
from taskpilot.state import TaskState, TaskStateUpdate


def initialize_task(state: TaskState) -> TaskStateUpdate:
    """Move a newly created task into the running state."""

    if state["status"] == TaskStatus.CREATED:
        return {"status": TaskStatus.RUNNING}
    return {}


def build_graph() -> CompiledStateGraph:
    """Build and compile the Phase 1 task lifecycle graph."""

    builder = StateGraph(TaskState)
    builder.add_node("initialize_task", initialize_task)
    builder.add_edge(START, "initialize_task")
    builder.add_edge("initialize_task", END)
    return builder.compile()
