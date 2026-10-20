"""Replanner Human Assistance 的 durable interrupt 与安全边界测试。"""

from dataclasses import dataclass, field
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.graph import build_graph, classify_execution_blocker
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    ReplanDecision,
    ReplanProposal,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    ToolCallRecord,
    ToolCallStatus,
    VerificationResult,
)
from taskpilot.policy import DefaultRiskPolicy
from taskpilot.persistence import (
    PersistenceConfig,
    SQLiteActionJournal,
    SQLiteCheckpointProvider,
)
from taskpilot.persistence.journal import ActionJournalLike
from taskpilot.service import (
    TaskConflictError,
    TaskPilotService,
    create_initial_state,
)
from taskpilot.tools import ToolRegistry


class FakeFunctionCallingModel:
    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.invocations: list[Sequence[BaseMessage]] = []

    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any):
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations.append(messages)
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


def finish(step: int) -> AIMessage:
    return tool_call(
        FINISH_STEP_NAME,
        {
            "summary": f"step {step} complete",
            "evidence": [f"verified evidence {step}"],
            "output": {"step": step},
        },
        f"finish-{step}",
    )


@dataclass
class LoginThenObserveTool:
    name: str = "browser_observe"
    description: str = "Read the current browser page"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    )
    metadata: dict[str, Any] = field(
        default_factory=lambda: {
            "source": "browser",
            "risk": "low",
            "replay_safe": True,
        }
    )
    calls: int = 0

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("login required before this page can be observed")
        return {"url": "https://example.test/private", "authenticated": True}


@dataclass
class NonReplaySafeWriteTool:
    name: str = "send_message"
    description: str = "Send one message"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        }
    )
    metadata: dict[str, Any] = field(
        default_factory=lambda: {
            "source": "local",
            "risk": "high",
            "replay_safe": False,
        }
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        return {"sent": True}


class FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(goal=user_input, completion_criteria=["both steps verified"])


class TwoStepPlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="prepare verified fact",
                    success_criteria=["prepared"],
                ),
                PlanStep(
                    id=2,
                    description="use authenticated page and send once",
                    depends_on=[1],
                    success_criteria=["sent"],
                ),
            ]
        )


class AcceptingVerifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        step = kwargs["step"]
        return StepVerificationResult(
            step_id=step.id,
            verified=True,
            checks=[
                CriterionCheck(criterion_index=0, satisfied=True, reason="fixture")
            ],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(completed=True, reason="fixture")


class RequestHumanReplanner:
    def __init__(self) -> None:
        self.calls = 0

    def replan(self, context: Any) -> ReplanProposal:
        self.calls += 1
        assert (
            context.content["failure_feedback"]["blocking_failure"]["retryable"]
            is False
        )
        return ReplanProposal(
            decision=ReplanDecision.REQUEST_HUMAN,
            reason="The authenticated browser session requires the user",
            required_human_action="Complete sign-in in the existing browser page",
            resume_condition="The page can be observed as authenticated",
        )


def interrupt_values(graph: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot = graph.get_state(config)
    return [item.value for task in snapshot.tasks for item in task.interrupts]


def make_graph_and_tools(
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    journal: ActionJournalLike | None = None,
) -> tuple[Any, LoginThenObserveTool, NonReplaySafeWriteTool]:
    observe = LoginThenObserveTool()
    write = NonReplaySafeWriteTool()
    registry = ToolRegistry()
    registry.register(observe)
    registry.register(write)
    model = FakeFunctionCallingModel(
        [
            finish(1),
            tool_call("browser_observe", {}, "observe-blocked"),
            tool_call("browser_observe", {}, "observe-after-human"),
            tool_call("send_message", {"message": "once"}, "send-once"),
            finish(2),
        ]
    )
    executor = TaskExecutor(
        registry,
        model=cast(BaseChatModel, model),
        policy=DefaultRiskPolicy(),
    )
    graph = build_graph(
        analyzer=FakeAnalyzer(),
        planner=TwoStepPlanner(),
        executor=executor,
        verifier=AcceptingVerifier(),
        replanner=RequestHumanReplanner(),
        checkpointer=checkpointer or MemorySaver(),
        action_journal=journal,
        max_replans=2,
        max_human_assists=1,
    )
    return graph, observe, write


@pytest.mark.asyncio
async def test_human_assistance_pause_resume_reobserves_and_keeps_hitl() -> None:
    graph, observe, write = make_graph_and_tools()
    config = {"configurable": {"thread_id": "human-assist"}, "recursion_limit": 100}

    paused = await graph.ainvoke(
        create_initial_state("fixture", task_id="human-assist", browser_enabled=True),
        config=config,
    )
    payload = interrupt_values(graph, config)[0]

    assert paused["status"] is TaskStatus.WAITING_HUMAN_ASSISTANCE
    assert payload["type"] == "human_assistance"
    assert payload["choices"] == ["completed", "cancel"]
    assert "sign-in" in payload["required_action"]
    assert observe.calls == 1
    assert [result.step_id for result in paused["step_results"]] == [1]

    approval_pause = await graph.ainvoke(
        Command(resume={"choice": "completed", "reason": "signed in"}),
        config=config,
    )

    assert approval_pause["status"] is TaskStatus.WAITING_APPROVAL
    assert interrupt_values(graph, config)[0]["type"] == "tool_approval"
    assert observe.calls == 2
    assert write.calls == []
    assert [result.step_id for result in approval_pause["step_results"]] == [1]

    final = await graph.ainvoke(Command(resume={"decision": "approve"}), config=config)

    assert final["status"] is TaskStatus.COMPLETED
    assert write.calls == [{"message": "once"}]
    assert [result.step_id for result in final["step_results"]] == [1, 2]
    assert final["human_assistance_count"] == 1
    assert final["replan_count"] == 1


@pytest.mark.asyncio
async def test_human_assistance_cancel_and_invalid_repeat_are_safe() -> None:
    graph, observe, write = make_graph_and_tools()
    config = {"configurable": {"thread_id": "human-cancel"}, "recursion_limit": 100}
    await graph.ainvoke(
        create_initial_state("fixture", task_id="human-cancel", browser_enabled=True),
        config=config,
    )

    canceled = await graph.ainvoke(
        Command(resume={"choice": "cancel", "reason": "stop"}),
        config=config,
    )
    repeated = await graph.ainvoke(
        Command(resume={"choice": "completed"}),
        config=config,
    )

    assert canceled["status"] is TaskStatus.FAILED
    assert repeated["status"] is TaskStatus.FAILED
    assert observe.calls == 1
    assert write.calls == []


def test_transient_failures_are_not_promoted_to_human_blockers() -> None:
    timeout = ToolCallRecord(
        call_id="timeout",
        step_id=1,
        tool_name="browser_observe",
        status=ToolCallStatus.FAILED,
        error="TimeoutError: network timed out",
    )
    assert classify_execution_blocker(timeout) is None


@pytest.mark.asyncio
async def test_sqlite_checkpoint_resumes_assistance_without_replaying_completed_step(
    tmp_path: Any,
) -> None:
    persistence = PersistenceConfig.from_state_dir(tmp_path)
    config = {"configurable": {"thread_id": "sqlite-assist"}, "recursion_limit": 100}
    async with (
        SQLiteCheckpointProvider(persistence) as checkpoint,
        SQLiteActionJournal(persistence.action_journal_db_path) as journal,
    ):
        graph, observe, write = make_graph_and_tools(
            checkpointer=checkpoint.checkpointer,
            journal=journal,
        )
        paused = await graph.ainvoke(
            create_initial_state(
                "fixture",
                task_id="sqlite-assist",
                browser_enabled=True,
            ),
            config=config,
        )
        resumed = await graph.ainvoke(
            Command(resume={"choice": "completed"}),
            config=config,
        )

        assert paused["status"] is TaskStatus.WAITING_HUMAN_ASSISTANCE
        assert resumed["status"] is TaskStatus.WAITING_APPROVAL
        assert [result.step_id for result in resumed["step_results"]] == [1]
        assert observe.calls == 2
        assert write.calls == []


@pytest.mark.asyncio
async def test_sqlite_checkpoint_survives_but_lost_browser_session_refuses_resume(
    tmp_path: Any,
) -> None:
    persistence = PersistenceConfig.from_state_dir(tmp_path)
    config = {"configurable": {"thread_id": "lost-browser"}, "recursion_limit": 100}
    async with (
        SQLiteCheckpointProvider(persistence) as checkpoint,
        SQLiteActionJournal(persistence.action_journal_db_path) as journal,
    ):
        graph, _, _ = make_graph_and_tools(
            checkpointer=checkpoint.checkpointer,
            journal=journal,
        )
        paused = await graph.ainvoke(
            create_initial_state(
                "fixture",
                task_id="lost-browser",
                browser_enabled=True,
            ),
            config=config,
        )
        assert paused["status"] is TaskStatus.WAITING_HUMAN_ASSISTANCE
        snapshot = await graph.aget_state(config)
        durable_interrupts = [
            item.value for task in snapshot.tasks for item in task.interrupts
        ]
        assert durable_interrupts[0]["requires_browser"] is True

    service = TaskPilotService(persistence, browser_allowed=True)
    with pytest.raises(TaskConflictError, match="live browser environment"):
        await service.resume_task(
            "lost-browser",
            interrupt_type="human_assistance",
            response={"choice": "completed"},
        )


def test_human_assistance_resume_cannot_smuggle_approval_or_action_fields() -> None:
    with pytest.raises(TaskConflictError, match="frozen action fields"):
        TaskPilotService._validate_resume_response(
            "human_assistance",
            {
                "choice": "completed",
                "decision": "approve",
                "arguments": {"message": "changed"},
            },
        )
