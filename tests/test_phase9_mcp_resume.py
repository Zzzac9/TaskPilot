"""真实 MCP STDIO provider 重建后的 durable Graph 恢复测试。"""

import sys
from pathlib import Path
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage

from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.graph import build_graph
from taskpilot.mcp import MCPServerConfig, MCPToolProvider
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    VerificationResult,
)
from taskpilot.persistence import (
    ExecutionFaultPoint,
    InjectedCrash,
    PersistenceConfig,
    SQLiteActionJournal,
    SQLiteCheckpointProvider,
)
from taskpilot.policy import DefaultRiskPolicy
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry


SERVER_PATH = Path(__file__).parent / "fixtures" / "mcp_server.py"


class FakeModel:
    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)

    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any):
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        return self.responses.pop(0)


def action(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": name,
        "args": arguments,
        "id": call_id,
        "type": "tool_call",
    }])


class Analyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(goal=user_input, completion_criteria=["echo observed"])


class Planner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(steps=[PlanStep(
            id=1,
            description="echo through rebuilt MCP provider",
            success_criteria=["echo result exists"],
        )])


class Verifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        return StepVerificationResult(
            step_id=1,
            verified=True,
            checks=[CriterionCheck(
                criterion_index=0,
                satisfied=True,
                reason="durable echo observation",
            )],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(completed=True, reason="MCP resume complete")


class CrashAfterTerminal:
    def hit(self, point: ExecutionFaultPoint, action_key: str) -> None:
        if point is ExecutionFaultPoint.AFTER_TERMINAL_JOURNAL_BEFORE_GRAPH_RETURN:
            raise InjectedCrash(action_key)


def initial_state(task_id: str) -> TaskState:
    return {
        "task_id": task_id,
        "user_input": "durable MCP echo",
        "task_spec": None,
        "plan": [],
        "current_step_id": None,
        "step_outcome": None,
        "step_results": [],
        "step_verification": None,
        "tool_calls": [],
        "task_results": {},
        "verification": None,
        "pending_action": None,
        "policy_decisions": [],
        "approval_records": [],
        "current_action_index": 0,
        "pending_executor_action": None,
        "recovery_issue": None,
        "status": TaskStatus.CREATED,
        "error": None,
    }


def server_config() -> MCPServerConfig:
    return MCPServerConfig(
        server_id="demo",
        transport="stdio",
        command=sys.executable,
        args=["-u", str(SERVER_PATH)],
        trust_tool_annotations=True,
    )


@pytest.mark.asyncio
async def test_real_mcp_provider_reconnects_for_same_durable_thread(
    tmp_path: Path,
) -> None:
    persistence = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "mcp-provider-rebuild"
    graph_config = {
        "configurable": {"thread_id": task_id},
        "recursion_limit": 100,
    }

    first_registry = ToolRegistry()
    async with MCPToolProvider([server_config()]) as mcp_provider:
        await mcp_provider.register_tools(first_registry)
        assert first_registry.get("demo__echo").metadata["replay_safe"] is True
        first_executor = TaskExecutor(
            first_registry,
            model=cast(BaseChatModel, FakeModel([
                action("demo__echo", {"text": "phase-9"}, "mcp-echo-1")
            ])),
            policy=DefaultRiskPolicy(),
        )
        async with SQLiteCheckpointProvider(persistence) as checkpoint, SQLiteActionJournal(
            persistence.action_journal_db_path
        ) as journal:
            graph = build_graph(
                analyzer=Analyzer(), planner=Planner(), executor=first_executor,
                verifier=Verifier(), checkpointer=checkpoint.checkpointer,
                action_journal=journal, fault_injector=CrashAfterTerminal(),
            )
            with pytest.raises(InjectedCrash):
                await graph.ainvoke(initial_state(task_id), config=graph_config)

    rebuilt_registry = ToolRegistry()
    reinvocations: list[dict[str, Any]] = []
    async with MCPToolProvider([server_config()]) as rebuilt_provider:
        adapters = await rebuilt_provider.register_tools(rebuilt_registry)
        echo_adapter = next(item for item in adapters if item.name == "demo__echo")
        original_invoke = echo_adapter.invoke

        async def counted_invoke(arguments: dict[str, Any]) -> Any:
            reinvocations.append(dict(arguments))
            return await original_invoke(arguments)

        echo_adapter.invoke = counted_invoke  # type: ignore[method-assign]
        rebuilt_executor = TaskExecutor(
            rebuilt_registry,
            model=cast(BaseChatModel, FakeModel([
                action(
                    FINISH_STEP_NAME,
                    {
                        "summary": "MCP echo is durable",
                        "evidence": ["journaled structured_content"],
                        "output": {"echoed": "phase-9"},
                    },
                    "mcp-finish-2",
                )
            ])),
            policy=DefaultRiskPolicy(),
        )
        async with SQLiteCheckpointProvider(persistence) as checkpoint, SQLiteActionJournal(
            persistence.action_journal_db_path
        ) as journal:
            graph = build_graph(
                analyzer=Analyzer(), planner=Planner(), executor=rebuilt_executor,
                verifier=Verifier(), checkpointer=checkpoint.checkpointer,
                action_journal=journal,
            )
            result = await graph.ainvoke(None, config=graph_config)

    assert reinvocations == []
    assert result["tool_calls"][0].result["structured_content"] == {
        "echoed": "phase-9"
    }
    assert result["status"] is TaskStatus.COMPLETED
