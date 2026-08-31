"""TaskPilot Phase 3 的 LangGraph 工作流。"""

from functools import partial

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from taskpilot.analyzer import TaskAnalyzer, TaskAnalyzerLike
from taskpilot.config import redact_sensitive_values
from taskpilot.models import PlanStepStatus, TaskStatus
from taskpilot.planner import TaskPlanner, TaskPlannerLike
from taskpilot.state import TaskState, TaskStateUpdate


def initialize_task(state: TaskState) -> TaskStateUpdate:
    """将新创建的任务推进到运行状态。"""

    # 只允许 created → running，避免意外覆盖完成、失败等其他状态。
    if state["status"] == TaskStatus.CREATED:
        return {"status": TaskStatus.RUNNING}
    return {}


def analyze_task(
    state: TaskState,
    *,
    analyzer: TaskAnalyzerLike,
) -> TaskStateUpdate:
    """调用 Analyzer，并将结构化结果写回任务状态。"""

    try:
        task_spec = analyzer.analyze(state["user_input"])
    except Exception as exc:
        # 当前阶段只记录清晰错误，不在这里引入重试或恢复机制。
        error_detail = redact_sensitive_values(str(exc))
        return {
            "status": TaskStatus.FAILED,
            "error": (
                f"Task Analyzer 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }
    return {"task_spec": task_spec, "error": None}


def plan_task(
    state: TaskState,
    *,
    planner: TaskPlannerLike,
) -> TaskStateUpdate:
    """调用 Planner，并将结构化步骤写回任务状态。"""

    task_spec = state["task_spec"]
    if task_spec is None:
        return {
            "status": TaskStatus.FAILED,
            "error": state["error"] or "Task Planner 无法运行: task_spec 缺失",
        }

    try:
        task_plan = planner.plan(task_spec)
    except Exception as exc:
        # 当前阶段只记录错误，不自动重试、重规划或恢复。
        error_detail = redact_sensitive_values(str(exc))
        return {
            "plan": [],
            "current_step_id": None,
            "status": TaskStatus.FAILED,
            "error": (
                f"Task Planner 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }

    current_step_id = next(
        (
            step.id
            for step in task_plan.steps
            if step.status == PlanStepStatus.PENDING
        ),
        None,
    )
    return {
        "plan": task_plan.steps,
        "current_step_id": current_step_id,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def build_graph(
    analyzer: TaskAnalyzerLike | None = None,
    planner: TaskPlannerLike | None = None,
) -> CompiledStateGraph:
    """构建并编译包含 Analyzer 与 Initial Planner 的 Phase 3 状态图。"""

    active_analyzer = analyzer if analyzer is not None else TaskAnalyzer()
    active_planner = planner if planner is not None else TaskPlanner()
    builder = StateGraph(TaskState)
    builder.add_node("initialize_task", initialize_task)
    builder.add_node(
        "analyze_task",
        partial(analyze_task, analyzer=active_analyzer),
    )
    builder.add_node(
        "plan_task",
        partial(plan_task, planner=active_planner),
    )
    # 当前阶段严格保持 START → initialize_task → analyze_task → plan_task → END。
    builder.add_edge(START, "initialize_task")
    builder.add_edge("initialize_task", "analyze_task")
    builder.add_edge("analyze_task", "plan_task")
    builder.add_edge("plan_task", END)
    return builder.compile()
