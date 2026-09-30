"""真实 LangGraph interrupt/Command resume 的 Phase 8 HITL 集成测试。"""

import json
from dataclasses import dataclass, field
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.graph import build_graph
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    VerificationResult,
)
from taskpilot.policy import DefaultRiskPolicy
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry


class FakeFunctionCallingModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.invocations: list[list[BaseMessage]] = []

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> "FakeFunctionCallingModel":
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations.append(list(messages))
        return self.responses.pop(0)


def tool_call(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": arguments,
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def finish_call(call_id: str = "finish") -> AIMessage:
    return tool_call(
        FINISH_STEP_NAME,
        {
            "summary": "The safe path produced enough deterministic evidence.",
            "evidence": ["The expected local Tool result was observed."],
            "output": {"completed": True},
        },
        call_id,
    )


@dataclass
class CountingTool:
    name: str
    risk: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    description: str = "Deterministic local HITL fixture"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "recipient": {"type": "string"},
                "message": {"type": "string"},
                "api_key": {"type": "string"},
                "payload": {"type": "object"},
            },
            "additionalProperties": False,
        }
    )

    @property
    def metadata(self) -> dict[str, str]:
        return {"source": "local", "risk": self.risk}

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        return {"call_count": len(self.calls), "received": dict(arguments)}


@dataclass
class FailingCountingTool(CountingTool):
    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        raise RuntimeError("controlled approved action failure")


class FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(
            goal=user_input,
            completion_criteria=["Finish one deterministic local step"],
        )


class FakePlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="Use the approved or safe local action",
                    success_criteria=["A safe result is available"],
                )
            ]
        )


class FakeVerifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        return StepVerificationResult(
            step_id=kwargs["step"].id,
            verified=True,
            checks=[
                CriterionCheck(
                    criterion_index=0,
                    satisfied=True,
                    reason="Deterministic fixture accepted",
                )
            ],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(completed=True, reason="Fixture task completed")


def initial_state(task_id: str) -> TaskState:
    return {
        "task_id": task_id,
        "user_input": "Run a deterministic HITL fixture",
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
        "status": TaskStatus.CREATED,
        "error": None,
    }


def make_graph(
    tools: Sequence[CountingTool],
    model: FakeFunctionCallingModel,
) -> CompiledStateGraph:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    executor = TaskExecutor(
        registry=registry,
        model=cast(BaseChatModel, model),
        policy=DefaultRiskPolicy(),
    )
    return build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor,
        verifier=FakeVerifier(),
        checkpointer=MemorySaver(),
    )


def interrupt_values(
    graph: CompiledStateGraph,
    config: dict[str, Any],
) -> list[Any]:
    snapshot = graph.get_state(config)
    return [item for task in snapshot.tasks for item in task.interrupts]


@pytest.mark.asyncio
async def test_real_interrupt_approve_executes_exact_action_once() -> None:
    tool = CountingTool(name="send_message", risk="high")
    exact_arguments = {"recipient": "alice", "message": "hello"}
    model = FakeFunctionCallingModel(
        [
            tool_call("send_message", exact_arguments, "send-1"),
            finish_call("finish-2"),
        ]
    )
    graph = make_graph([tool], model)
    config = {"configurable": {"thread_id": "hitl-approve"}}

    paused = await graph.ainvoke(initial_state("hitl-approve"), config=config)

    assert tool.calls == []
    assert paused["status"] is TaskStatus.WAITING_APPROVAL
    assert paused["pending_action"].arguments == exact_arguments
    interrupts = interrupt_values(graph, config)
    assert len(interrupts) == 1
    assert interrupts[0].value["tool_name"] == "send_message"
    assert interrupts[0].value["arguments_preview"] == exact_arguments

    final = await graph.ainvoke(
        Command(resume={"decision": "approve"}),
        config=config,
    )

    assert tool.calls == [exact_arguments]
    assert final["status"] is TaskStatus.COMPLETED
    assert final.get("pending_action") is None
    assert len(final["tool_calls"]) == 1
    assert final["tool_calls"][0].call_id == "send-1"
    assert final["approval_records"][0].decision.value == "approve"
    print(
        "HITL_APPROVE_SMOKE="
        + json.dumps(
            {
                "paused_status": paused["status"].value,
                "interrupt": interrupts[0].value,
                "counter_before": 0,
                "counter_after": len(tool.calls),
                "actual_arguments": tool.calls[0],
                "final_status": final["status"].value,
            },
            ensure_ascii=True,
        )
    )


@pytest.mark.asyncio
async def test_reject_never_executes_high_risk_and_can_use_low_risk_alternative() -> None:
    high = CountingTool(name="local_write", risk="high")
    low = CountingTool(name="local_read", risk="low")
    model = FakeFunctionCallingModel(
        [
            tool_call("local_write", {"message": "blocked"}, "write-1"),
            tool_call("local_read", {"message": "safe"}, "read-2"),
            finish_call("finish-3"),
        ]
    )
    graph = make_graph([high, low], model)
    config = {"configurable": {"thread_id": "hitl-reject"}}

    paused = await graph.ainvoke(initial_state("hitl-reject"), config=config)
    final = await graph.ainvoke(
        Command(resume={"decision": "reject", "reason": "Use read-only path"}),
        config=config,
    )

    assert paused["status"] is TaskStatus.WAITING_APPROVAL
    assert high.calls == []
    assert low.calls == [{"message": "safe"}]
    assert final["status"] is TaskStatus.COMPLETED
    assert [record.tool_name for record in final["tool_calls"]] == ["local_read"]
    assert final["approval_records"][0].decision.value == "reject"
    second_context = json.loads(
        cast(HumanMessage, model.invocations[1][1]).content
    )
    assert second_context["approval_history"][0]["reason"] == "Use read-only path"
    print(
        "HITL_REJECT_ALTERNATIVE_SMOKE="
        + json.dumps(
            {
                "high_risk_calls": len(high.calls),
                "low_risk_calls": len(low.calls),
                "approval": final["approval_records"][0].model_dump(mode="json"),
                "final_status": final["status"].value,
            },
            ensure_ascii=True,
        )
    )


@pytest.mark.asyncio
async def test_sensitive_preview_redacted_but_approved_tool_receives_originals() -> None:
    tool = CountingTool(name="secure_send", risk="high")
    arguments = {
        "recipient": "alice",
        "api_key": "super-secret",
        "payload": {"password": "private", "message": "hello"},
    }
    model = FakeFunctionCallingModel(
        [tool_call("secure_send", arguments, "secure-1"), finish_call("finish-2")]
    )
    graph = make_graph([tool], model)
    config = {"configurable": {"thread_id": "hitl-secret"}}

    await graph.ainvoke(initial_state("hitl-secret"), config=config)
    preview = interrupt_values(graph, config)[0].value["arguments_preview"]
    assert preview == {
        "recipient": "alice",
        "api_key": "[REDACTED]",
        "payload": {"password": "[REDACTED]", "message": "hello"},
    }
    assert "super-secret" not in json.dumps(preview)
    assert "private" not in json.dumps(preview)

    final = await graph.ainvoke(
        Command(resume={"decision": "approve"}),
        config=config,
    )

    assert tool.calls == [arguments]
    assert final["status"] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_invalid_resume_cannot_edit_or_execute_pending_action() -> None:
    tool = CountingTool(name="local_write", risk="high")
    model = FakeFunctionCallingModel(
        [tool_call("local_write", {"message": "original"}, "edit-1")]
    )
    graph = make_graph([tool], model)
    config = {"configurable": {"thread_id": "hitl-invalid"}}

    await graph.ainvoke(initial_state("hitl-invalid"), config=config)
    result = await graph.ainvoke(
        Command(
            resume={
                "decision": "approve",
                "arguments": {"message": "modified"},
            }
        ),
        config=config,
    )

    assert tool.calls == []
    assert result["status"] is TaskStatus.FAILED
    assert "resume value 无效" in result["error"]


@pytest.mark.asyncio
async def test_approved_tool_environment_error_is_recorded_and_can_recover() -> None:
    failing = FailingCountingTool(name="high_failing_write", risk="high")
    low = CountingTool(name="local_read", risk="low")
    model = FakeFunctionCallingModel(
        [
            tool_call(
                "high_failing_write",
                {"message": "attempt once"},
                "failing-1",
            ),
            tool_call("local_read", {"message": "fallback"}, "fallback-2"),
            finish_call("finish-3"),
        ]
    )
    graph = make_graph([failing, low], model)
    config = {"configurable": {"thread_id": "hitl-approved-failure"}}

    await graph.ainvoke(initial_state("hitl-approved-failure"), config=config)
    final = await graph.ainvoke(
        Command(resume={"decision": "approve"}),
        config=config,
    )

    assert failing.calls == [{"message": "attempt once"}]
    assert low.calls == [{"message": "fallback"}]
    assert [record.status.value for record in final["tool_calls"]] == [
        "failed",
        "success",
    ]
    assert final["status"] is TaskStatus.COMPLETED
