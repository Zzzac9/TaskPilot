"""Phase 2 状态图的离线 smoke test。"""

from taskpilot.graph import build_graph
from taskpilot.models import PlanStep, TaskPlan, TaskSpec, TaskStatus
from taskpilot.state import TaskState


class StubAnalyzer:
    """避免 smoke test 访问真实模型。"""

    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(
            goal=user_input,
            completion_criteria=["返回符合用户要求的结果"],
        )


class StubPlanner:
    """返回一个最小且有效的离线计划。"""

    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="Prepare the requested result",
                    success_criteria=["The requested result is prepared"],
                )
            ]
        )


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

    result = build_graph(
        analyzer=StubAnalyzer(),
        planner=StubPlanner(),
    ).invoke(initial_state)

    assert result["task_id"] == initial_state["task_id"]
    assert result["user_input"] == initial_state["user_input"]
    assert result["status"] is TaskStatus.RUNNING
    assert result["task_spec"] is not None
    assert len(result["plan"]) == 1
    assert result["current_step_id"] == 1
