"""早期 step-level Executor 的 LangGraph 装配，仅用于向后兼容。

生产环境由 graph.py 的 action-level Graph 执行。旧 Graph 不支持新的
Action Journal / durable action 边界，切勿用它替代生产路径。
"""

from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from taskpilot.analyzer import TaskAnalyzerLike
from taskpilot.executor import TaskExecutorLike
from taskpilot.planner import TaskPlannerLike
from taskpilot.state import TaskState
from taskpilot.verifier import TaskVerifierLike


def build_legacy_graph(
    *,
    analyzer: TaskAnalyzerLike,
    planner: TaskPlannerLike,
    executor: TaskExecutorLike,
    verifier: TaskVerifierLike,
    max_step_attempts: int,
    checkpointer: BaseCheckpointSaver[Any] | None,
    nodes: Mapping[str, Callable[..., Any]],
) -> CompiledStateGraph:
    """只负责连接旧节点，不再包含业务实现或生产组件的默认构造。"""

    # nodes 由 graph.build_graph 显式注入，防止与生产路径形成循环导入。
    builder = StateGraph(TaskState)
    builder.add_node("initialize_task", nodes["initialize_task"])
    builder.add_node(
        "analyze_task",
        partial(nodes["analyze_task"], analyzer=analyzer),
    )
    builder.add_node("plan_task", partial(nodes["plan_task"], planner=planner))
    builder.add_node(
        "execute_step",
        partial(nodes["execute_step"], executor=executor),
    )
    builder.add_node("prepare_approval", nodes["prepare_approval"])
    builder.add_node("human_approval", nodes["human_approval"])
    builder.add_node(
        "execute_approved_action",
        partial(nodes["execute_approved_action"], executor=executor),
    )
    builder.add_node("handle_rejection", nodes["handle_rejection"])
    builder.add_node(
        "verify_step",
        partial(
            nodes["verify_step"],
            verifier=verifier,
            max_step_attempts=max_step_attempts,
        ),
    )
    builder.add_node("advance_step", nodes["advance_step"])
    builder.add_node(
        "verify_task",
        partial(nodes["verify_task"], verifier=verifier),
    )
    builder.add_node("finalize_terminal", nodes["finalize_terminal"])

    builder.add_edge(START, "initialize_task")
    builder.add_edge("initialize_task", "analyze_task")
    builder.add_edge("analyze_task", "plan_task")
    builder.add_conditional_edges(
        "plan_task",
        nodes["_route_after_plan"],
        {"execute_step": "execute_step", "end": "finalize_terminal"},
    )
    builder.add_conditional_edges(
        "execute_step",
        nodes["_route_after_execute"],
        {
            "prepare_approval": "prepare_approval",
            "verify_step": "verify_step",
            "end": "finalize_terminal",
        },
    )
    builder.add_edge("prepare_approval", "human_approval")
    builder.add_conditional_edges(
        "human_approval",
        nodes["_route_after_human_approval"],
        {
            "execute_approved_action": "execute_approved_action",
            "handle_rejection": "handle_rejection",
            "end": "finalize_terminal",
        },
    )
    builder.add_edge("execute_approved_action", "execute_step")
    builder.add_edge("handle_rejection", "execute_step")
    builder.add_conditional_edges(
        "verify_step",
        nodes["_route_after_step_verification"],
        {
            "advance_step": "advance_step",
            "execute_step": "execute_step",
            "end": "finalize_terminal",
        },
    )
    builder.add_conditional_edges(
        "advance_step",
        nodes["_route_after_advance"],
        {
            "execute_step": "execute_step",
            "verify_task": "verify_task",
            "end": "finalize_terminal",
        },
    )
    builder.add_conditional_edges(
        "verify_task",
        nodes["_route_after_terminal_verification"],
        {"failure": "finalize_terminal", "end": END},
    )
    builder.add_edge("finalize_terminal", END)
    return builder.compile(checkpointer=checkpointer)
