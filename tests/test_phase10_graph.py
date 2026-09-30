"""Phase 10 Dynamic Replanning、Context 与 Trace 的 action-level smoke tests。"""

from dataclasses import dataclass, field
import json
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage

from taskpilot.cli import create_initial_state
from taskpilot.context import ContextBuilder, ContextConfig
from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.graph import build_graph
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    PlanStepStatus,
    ReplanProposal,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    VerificationResult,
)
from taskpilot.persistence import InMemoryActionJournal
from taskpilot.trace import (
    InMemoryTraceRecorder,
    SQLiteTraceRecorder,
    TraceEventType,
    summarize_task_trace,
)
from taskpilot.tools import ToolRegistry


class FakeFunctionCallingModel:
    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.messages: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any):
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.messages.append(list(messages))
        return self.responses.pop(0)


def finish(call_id: str, step: int) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": FINISH_STEP_NAME,
                "args": {
                    "summary": f"step {step} claimed",
                    "evidence": [f"evidence-{step}"],
                    "output": {"step": step},
                },
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def tool_action(call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "unused",
                "args": {},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


@dataclass
class DummyTool:
    name: str = "unused"
    description: str = "Keeps registry non-empty for finish-only smoke tests"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    metadata: dict[str, Any] = field(
        default_factory=lambda: {"source": "local", "risk": "low", "replay_safe": True}
    )
    calls: int = 0

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, bool]:
        self.calls += 1
        return {"unused": True}


class FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(goal=user_input, constraints={"offline": True}, completion_criteria=["done"])


class FakePlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="initial path",
                    success_criteria=["fact found"],
                    revision=99,
                )
            ]
        )


class ThreeStepPlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(id=1, description="discover candidates", success_criteria=["candidates found"]),
                PlanStep(
                    id=2,
                    description="use broken strategy",
                    depends_on=[1],
                    success_criteria=["strategy succeeds"],
                ),
                PlanStep(
                    id=3,
                    description="old remaining work",
                    depends_on=[2],
                    success_criteria=["old path done"],
                ),
            ]
        )


class FakeReplanner:
    def __init__(self, proposals: Sequence[ReplanProposal]) -> None:
        self.proposals = list(proposals)
        self.contexts = []

    def replan(self, context):
        self.contexts.append(context)
        return self.proposals.pop(0)


class StepFailureVerifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        step = kwargs["step"]
        accepted = step.id != 2
        return StepVerificationResult(
            step_id=step.id,
            verified=accepted,
            checks=[
                CriterionCheck(
                    criterion_index=0,
                    satisfied=accepted,
                    reason="accepted" if accepted else "initial path blocked",
                )
            ],
            feedback=None if accepted else "try a different semantic path",
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(completed=True, reason="replacement completed")


class TaskRejectionVerifier:
    def __init__(self) -> None:
        self.task_calls = 0

    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        step = kwargs["step"]
        return StepVerificationResult(
            step_id=step.id,
            verified=True,
            checks=[CriterionCheck(criterion_index=0, satisfied=True, reason="accepted")],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        self.task_calls += 1
        if self.task_calls == 1:
            return VerificationResult(
                completed=False,
                reason="final format missing",
                missing_requirements=["produce final format"],
                next_action="replan remaining work",
            )
        return VerificationResult(completed=True, reason="all criteria satisfied")


def executor(responses: Sequence[AIMessage], model_out: list[FakeFunctionCallingModel]) -> TaskExecutor:
    registry = ToolRegistry()
    registry.register(DummyTool())
    model = FakeFunctionCallingModel(responses)
    model_out.append(model)
    return TaskExecutor(registry=registry, model=cast(BaseChatModel, model))


def initial(task_id: str):
    state = create_initial_state("complete an offline task")
    state["task_id"] = task_id
    return state


@pytest.mark.asyncio
async def test_step_failure_replans_with_fresh_id_and_preserves_history() -> None:
    models: list[FakeFunctionCallingModel] = []
    replanner = FakeReplanner(
        [
            ReplanProposal(
                reason="use remaining alternative",
                steps=[
                    PlanStep(
                        id=4,
                        description="alternative path",
                        depends_on=[1],
                        success_criteria=["fact recovered"],
                    ),
                    PlanStep(
                        id=5,
                        description="finish remaining work",
                        depends_on=[4],
                        success_criteria=["remaining work complete"],
                    ),
                ],
            )
        ]
    )
    trace = InMemoryTraceRecorder()
    journal = InMemoryActionJournal()
    graph = build_graph(
        analyzer=FakeAnalyzer(),
        planner=ThreeStepPlanner(),
        executor=executor(
            [
                tool_action("tool-step-1"),
                finish("finish-1", 1),
                tool_action("tool-step-2"),
                finish("finish-2", 2),
                tool_action("tool-step-4"),
                finish("finish-4", 4),
                finish("finish-5", 5),
            ],
            models,
        ),
        verifier=StepFailureVerifier(),
        replanner=replanner,
        max_step_attempts=1,
        max_replans=2,
        trace_recorder=trace,
        action_journal=journal,
    )
    result = await graph.ainvoke(
        initial("step-replan"), config={"recursion_limit": 100}
    )

    assert result["status"] is TaskStatus.COMPLETED
    assert [(item.id, item.revision, item.status) for item in result["plan"]] == [
        (1, 0, PlanStepStatus.COMPLETED),
        (2, 0, PlanStepStatus.FAILED),
        (3, 0, PlanStepStatus.SKIPPED),
        (4, 1, PlanStepStatus.COMPLETED),
        (5, 1, PlanStepStatus.COMPLETED),
    ]
    assert [item.step_id for item in result["step_results"]] == [1, 4, 5]
    assert result["plan_revision"] == 1
    assert result["replan_count"] == 1
    assert result["replan_history"][0].replaced_step_ids == [2, 3]
    assert result["replan_history"][0].new_step_ids == [4, 5]
    assert replanner.contexts[0].content["historical_max_step_id"] == 3
    assert set(journal.records) == {
        "step-replan:1:0:1",
        "step-replan:2:0:1",
        "step-replan:4:0:1",
    }
    replan_event = next(
        event
        for event in await trace.list_events("step-replan")
        if event.event_type is TraceEventType.PLAN_REPLANNED
    )
    assert replan_event.plan_revision == 1
    assert replan_event.payload["new_revision"] == 1
    print(
        "REPLAN_TRACE_REVISION_SMOKE="
        + json.dumps(
            {
                "payload_revision": replan_event.payload["new_revision"],
                "trace_event_revision": replan_event.plan_revision,
            },
            sort_keys=True,
        )
    )
    print(
        "REPLAN_STEP_FAILURE_SMOKE="
        + json.dumps(
            {
                "initial_revision": 0,
                "failed_step_id": 2,
                "replan_trigger": result["replan_history"][0].trigger.value,
                "replan_count": result["replan_count"],
                "new_revision": result["plan_revision"],
                "new_step_ids": result["replan_history"][0].new_step_ids,
                "completed_old_step_rerun": False,
                "task_status": result["status"].value,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
async def test_task_verification_replans_and_reuses_completed_result() -> None:
    models: list[FakeFunctionCallingModel] = []
    verifier = TaskRejectionVerifier()
    replanner = FakeReplanner(
        [
            ReplanProposal(
                reason="add missing final format",
                steps=[
                    PlanStep(
                        id=2,
                        description="produce final format",
                        depends_on=[1],
                        success_criteria=["format produced"],
                    )
                ],
            )
        ]
    )
    graph = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor([finish("finish-a", 1), finish("finish-b", 2)], models),
        verifier=verifier,
        replanner=replanner,
        max_replans=2,
    )
    result = await graph.ainvoke(initial("task-replan"))

    assert result["status"] is TaskStatus.COMPLETED
    assert [item.step_id for item in result["step_results"]] == [1, 2]
    assert result["plan"][0].status is PlanStepStatus.COMPLETED
    assert result["plan"][0].revision == 0
    assert result["plan"][1].depends_on == [1]
    assert replanner.contexts[0].content["verified_step_results"][0]["step_id"] == 1
    assert replanner.contexts[0].content["failure_feedback"]["missing_requirements"] == [
        "produce final format"
    ]
    print(
        "TASK_VERIFICATION_REPLAN_SMOKE="
        + json.dumps(
            {
                "first_task_verification": False,
                "missing_requirements": ["produce final format"],
                "replan_count": result["replan_count"],
                "second_task_verification": result["verification"].completed,
                "task_status": result["status"].value,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
async def test_max_replans_zero_preserves_fail_fast() -> None:
    models: list[FakeFunctionCallingModel] = []
    replanner = FakeReplanner([])
    graph = build_graph(
        analyzer=FakeAnalyzer(), planner=FakePlanner(),
        executor=executor([finish("finish", 1)], models), verifier=type(
            "AlwaysReject",
            (),
            {
                "verify_step": lambda self, **kwargs: StepVerificationResult(
                    step_id=kwargs["step"].id,
                    verified=False,
                    checks=[CriterionCheck(criterion_index=0, satisfied=False, reason="no")],
                    feedback="blocked",
                ),
                "verify_task": lambda self, **kwargs: VerificationResult(completed=False, reason="unused"),
            },
        )(),
        replanner=replanner, max_step_attempts=1, max_replans=0,
    )
    result = await graph.ainvoke(initial("no-replan"))
    assert result["status"] is TaskStatus.FAILED
    assert "max_step_attempts=1" in result["error"]
    assert replanner.contexts == []


@pytest.mark.asyncio
async def test_replan_budget_is_bounded() -> None:
    models: list[FakeFunctionCallingModel] = []
    replanner = FakeReplanner(
        [ReplanProposal(reason="once", steps=[PlanStep(id=2, description="second", success_criteria=["done"])])]
    )
    graph = build_graph(
        analyzer=FakeAnalyzer(), planner=FakePlanner(),
        executor=executor([finish("one", 1), finish("two", 2)], models),
        verifier=StepFailureVerifier(), replanner=replanner,
        max_step_attempts=1, max_replans=1,
    )
    # 此 Verifier 会接受 id=2；换成始终拒绝以触发第二次预算检查。
    graph_verifier = type(
        "AlwaysReject",
        (),
        {
            "verify_step": lambda self, **kwargs: StepVerificationResult(
                step_id=kwargs["step"].id,
                verified=False,
                checks=[CriterionCheck(criterion_index=0, satisfied=False, reason="no")],
                feedback="still blocked",
            ),
            "verify_task": lambda self, **kwargs: VerificationResult(completed=False, reason="unused"),
        },
    )()
    graph = build_graph(
        analyzer=FakeAnalyzer(), planner=FakePlanner(),
        executor=executor([finish("one-b", 1), finish("two-b", 2)], models),
        verifier=graph_verifier, replanner=replanner,
        max_step_attempts=1, max_replans=1,
    )
    result = await graph.ainvoke(initial("bounded-replan"))
    assert result["status"] is TaskStatus.FAILED
    assert result["replan_count"] == 1
    assert "replan budget exhausted" in result["error"]


@pytest.mark.asyncio
async def test_trace_smoke_records_context_decision_and_final_state() -> None:
    models: list[FakeFunctionCallingModel] = []
    trace = InMemoryTraceRecorder()
    graph = build_graph(
        analyzer=FakeAnalyzer(), planner=FakePlanner(),
        executor=executor([finish("finish", 1)], models), verifier=TaskRejectionVerifier(),
        max_replans=0, trace_recorder=trace,
    )
    # TaskRejectionVerifier 会在 Task 层拒绝；这里改用其第二次调用语义。
    verifier = TaskRejectionVerifier()
    verifier.task_calls = 1
    graph = build_graph(
        analyzer=FakeAnalyzer(), planner=FakePlanner(),
        executor=executor([finish("finish-ok", 1)], models), verifier=verifier,
        max_replans=0, trace_recorder=trace,
    )
    result = await graph.ainvoke(initial("trace-smoke"))
    event_types = [event.event_type for event in await trace.list_events("trace-smoke")]
    assert result["status"] is TaskStatus.COMPLETED
    assert {
        TraceEventType.TASK_STARTED,
        TraceEventType.TASK_ANALYZED,
        TraceEventType.PLAN_CREATED,
        TraceEventType.STEP_STARTED,
        TraceEventType.CONTEXT_BUILT,
        TraceEventType.LLM_ACTION_DECIDED,
        TraceEventType.STEP_VERIFIED,
        TraceEventType.TASK_VERIFIED,
        TraceEventType.TASK_COMPLETED,
    } <= set(event_types)
    events = await trace.list_events("trace-smoke")
    print(
        "TRACE_SMOKE="
        + json.dumps(
            {
                "event_types": [event.event_type.value for event in events],
                "latencies_ms": [
                    event.latency_ms for event in events if event.latency_ms is not None
                ],
                "event_count": len(events),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
async def test_tool_trace_records_start_and_terminal_result() -> None:
    models: list[FakeFunctionCallingModel] = []
    trace = InMemoryTraceRecorder()
    verifier = TaskRejectionVerifier()
    verifier.task_calls = 1
    graph = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor([tool_action("tool-1"), finish("finish-2", 1)], models),
        verifier=verifier,
        max_replans=0,
        trace_recorder=trace,
    )
    result = await graph.ainvoke(initial("tool-trace"))
    events = await trace.list_events("tool-trace")
    types = [event.event_type for event in events]
    assert result["status"] is TaskStatus.COMPLETED
    assert TraceEventType.TOOL_STARTED in types
    assert TraceEventType.TOOL_SUCCEEDED in types
    terminal = next(event for event in events if event.event_type is TraceEventType.TOOL_SUCCEEDED)
    assert terminal.latency_ms is not None
    assert terminal.latency_ms >= 0


@pytest.mark.asyncio
async def test_sqlite_trace_graph_smoke_reopens_and_summarizes(tmp_path) -> None:
    models: list[FakeFunctionCallingModel] = []
    path = tmp_path / "traces.sqlite3"
    verifier = TaskRejectionVerifier()
    verifier.task_calls = 1
    async with SQLiteTraceRecorder(path) as recorder:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=executor([tool_action("sqlite-tool"), finish("sqlite-finish", 1)], models),
            verifier=verifier, max_replans=0, trace_recorder=recorder,
        )
        result = await graph.ainvoke(initial("sqlite-trace-smoke"))
    async with SQLiteTraceRecorder(path) as reopened:
        summary = await summarize_task_trace(reopened, "sqlite-trace-smoke")
        events = await reopened.list_events("sqlite-trace-smoke")
    assert result["status"] is TaskStatus.COMPLETED
    assert summary.tool_call_count == 1
    assert any(event.event_type is TraceEventType.TOOL_SUCCEEDED for event in events)
    print(
        "TRACE_SMOKE="
        + json.dumps(
            {
                "task_id": "sqlite-trace-smoke",
                "tool_calls": summary.tool_call_count,
                "tool_failures": summary.tool_failure_count,
                "step_rejections": summary.step_rejection_count,
                "replans": summary.replan_count,
                "approval_requests": summary.approval_request_count,
                "recovery_required": summary.recovery_required_count,
                "trace_reopened": True,
                "event_count": summary.event_count,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def test_negative_max_replans_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_replans"):
        build_graph(max_replans=-1)


@pytest.mark.asyncio
async def test_context_overflow_is_graph_failure_without_model_or_tool_call() -> None:
    registry = ToolRegistry()
    tool = DummyTool()
    registry.register(tool)
    model = FakeFunctionCallingModel([finish("must-not-run", 1)])
    active_executor = TaskExecutor(
        registry=registry,
        model=cast(BaseChatModel, model),
        context_builder=ContextBuilder(ContextConfig(max_total_chars=50)),
    )
    verifier = TaskRejectionVerifier()
    verifier.task_calls = 1
    graph = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=active_executor,
        verifier=verifier,
        max_replans=0,
    )
    result = await graph.ainvoke(initial("context-overflow"))
    assert result["status"] is TaskStatus.FAILED
    assert "context budget" in result["error"].lower()
    assert model.messages == []
    assert tool.calls == 0
    print(
        "CONTEXT_OVERFLOW_GRAPH_SMOKE="
        + json.dumps(
            {
                "model_invoked": False,
                "task_status": result["status"].value,
                "graph_exception": False,
            },
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
async def test_successful_replan_trace_revision_increments_twice() -> None:
    models: list[FakeFunctionCallingModel] = []
    trace = InMemoryTraceRecorder()
    replanner = FakeReplanner(
        [
            ReplanProposal(
                reason="revision one",
                steps=[PlanStep(id=2, description="second", success_criteria=["done"])],
            ),
            ReplanProposal(
                reason="revision two",
                steps=[PlanStep(id=3, description="third", success_criteria=["done"])],
            ),
        ]
    )
    always_reject = type(
        "AlwaysRejectForTwoReplans",
        (),
        {
            "verify_step": lambda self, **kwargs: StepVerificationResult(
                step_id=kwargs["step"].id,
                verified=False,
                checks=[CriterionCheck(criterion_index=0, satisfied=False, reason="no")],
                feedback="try another revision",
            ),
            "verify_task": lambda self, **kwargs: VerificationResult(
                completed=False, reason="unused"
            ),
        },
    )()
    graph = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor(
            [finish("revision-0", 1), finish("revision-1", 2), finish("revision-2", 3)],
            models,
        ),
        verifier=always_reject,
        replanner=replanner,
        max_step_attempts=1,
        max_replans=2,
        trace_recorder=trace,
    )
    result = await graph.ainvoke(initial("two-revisions"), config={"recursion_limit": 100})
    events = [
        event
        for event in await trace.list_events("two-revisions")
        if event.event_type is TraceEventType.PLAN_REPLANNED
        and event.status == "success"
    ]
    assert result["status"] is TaskStatus.FAILED
    assert result["replan_count"] == 2
    assert [event.plan_revision for event in events] == [1, 2]
    assert [event.payload["new_revision"] for event in events] == [1, 2]
