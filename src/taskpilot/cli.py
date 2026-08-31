"""Minimal command-line smoke test for TaskPilot."""

import argparse
from typing import Sequence
from uuid import uuid4

from taskpilot.graph import build_graph
from taskpilot.models import TaskStatus
from taskpilot.state import TaskState


def create_initial_state(user_input: str) -> TaskState:
    """Create the structured initial state for a user request."""

    return {
        "task_id": str(uuid4()),
        "user_input": user_input,
        "task_spec": None,
        "plan": [],
        "current_step_id": None,
        "tool_calls": [],
        "task_results": {},
        "verification": None,
        "status": TaskStatus.CREATED,
        "error": None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Phase 1 graph once and print its identity and status."""

    parser = argparse.ArgumentParser(description="Run the TaskPilot Phase 1 graph.")
    parser.add_argument("task", help="Task description")
    args = parser.parse_args(argv)

    result = build_graph().invoke(create_initial_state(args.task))
    status = result["status"]
    status_value = status.value if isinstance(status, TaskStatus) else str(status)
    print(f"task_id={result['task_id']}")
    print(f"status={status_value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

