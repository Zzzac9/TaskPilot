"""Phase 10 对 Phase 8/9 approval、recovery、journal 边界的 Trace 测试。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from taskpilot.cli import create_initial_state
from taskpilot.executor import TaskExecutor
from taskpilot.graph import (
    canonical_arguments_hash,
    execute_tool_action,
    human_approval_traced,
    prepare_approval_traced,
    recovery_review_traced,
)
from taskpilot.models import (
    ExecutorAction,
    ExecutorActionType,
    PlanStep,
    PlanStepStatus,
    TaskSpec,
    TaskStatus,
)
from taskpilot.persistence import (
    ActionJournalRecord,
    InMemoryActionJournal,
    JournalStatus,
    RecoveryIssue,
)
from taskpilot.persistence.models import NoOpExecutionFaultInjector
from taskpilot.policy import (
    PendingToolAction,
    PolicyDecision,
    PolicyOutcome,
    RiskLevel,
)
from taskpilot.trace import InMemoryTraceRecorder, TraceEventType
from taskpilot.tools import ToolRegistry


@dataclass
class BoundaryTool:
    replay_safe: bool
    fail: bool = False
    calls: int = 0
    name: str = "boundary"
    description: str = "Offline trace boundary tool"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        }
    )

    @property
    def metadata(self) -> dict[str, Any]:
        return {"source": "local", "risk": "low", "replay_safe": self.replay_safe}

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, int]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("controlled failure")
        return {"value": arguments["value"]}


def boundary_state(*, replay_safe: bool = True):
    state = create_initial_state("trace boundary")
    state.update(
        {
            "task_id": "boundary-task",
            "task_spec": TaskSpec(goal="trace boundary"),
            "plan": [
                PlanStep(
                    id=1,
                    description="boundary",
                    status=PlanStepStatus.RUNNING,
                    success_criteria=["observed"],
                )
            ],
            "current_step_id": 1,
            "status": TaskStatus.RUNNING,
            "pending_executor_action": ExecutorAction(
                step_id=1,
                action_index=1,
                call_id="call-1",
                action_type=ExecutorActionType.TOOL,
                tool_name="boundary",
                arguments={"value": 7},
            ),
        }
    )
    return state


def make_executor(tool: BoundaryTool) -> TaskExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return TaskExecutor(registry)


def terminal_record(status: JournalStatus, replay_safe: bool = True) -> ActionJournalRecord:
    now = datetime.now(UTC)
    return ActionJournalRecord(
        action_key="boundary-task:1:0:1",
        task_id="boundary-task",
        step_id=1,
        retry_count=0,
        action_index=1,
        call_id="call-1",
        tool_name="boundary",
        arguments_hash=canonical_arguments_hash({"value": 7}),
        status=status,
        replay_safe=replay_safe,
        result={"value": 7} if status is JournalStatus.SUCCEEDED else None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_terminal_journal_materialization_is_traced_without_reinvoke() -> None:
    state = boundary_state()
    tool = BoundaryTool(replay_safe=True)
    journal = InMemoryActionJournal()
    journal.records["boundary-task:1:0:1"] = terminal_record(JournalStatus.SUCCEEDED)
    trace = InMemoryTraceRecorder()
    update = await execute_tool_action(
        state,
        executor=make_executor(tool),
        journal=journal,
        fault_injector=NoOpExecutionFaultInjector(),
        trace_recorder=trace,
    )
    types = [event.event_type for event in await trace.list_events("boundary-task")]
    assert tool.calls == 0
    assert update["tool_calls"][0].result == {"value": 7}
    assert types == [TraceEventType.TOOL_MATERIALIZED_FROM_JOURNAL]


@pytest.mark.asyncio
async def test_unsafe_started_requires_recovery_not_replanning() -> None:
    state = boundary_state(replay_safe=False)
    tool = BoundaryTool(replay_safe=False)
    journal = InMemoryActionJournal()
    journal.records["boundary-task:1:0:1"] = terminal_record(
        JournalStatus.STARTED, replay_safe=False
    )
    trace = InMemoryTraceRecorder()
    update = await execute_tool_action(
        state,
        executor=make_executor(tool),
        journal=journal,
        fault_injector=NoOpExecutionFaultInjector(),
        trace_recorder=trace,
    )
    assert update["status"] is TaskStatus.INTERRUPTED
    assert "pending_replan" not in update
    assert tool.calls == 0
    assert (await trace.list_events("boundary-task"))[0].event_type is TraceEventType.RECOVERY_REQUIRED


@pytest.mark.asyncio
async def test_recovery_abort_is_traced_and_does_not_replan(monkeypatch) -> None:
    state = boundary_state(replay_safe=False)
    state["recovery_issue"] = RecoveryIssue(
        action_key="boundary-task:1:0:1",
        task_id="boundary-task",
        step_id=1,
        action_index=1,
        tool_name="boundary",
        call_id="call-1",
        reason="ambiguous",
        replay_safe=False,
    )
    monkeypatch.setattr(
        "taskpilot.graph.interrupt",
        lambda payload: {"choice": "abort", "reason": "operator abort"},
    )
    trace = InMemoryTraceRecorder()
    update = await recovery_review_traced(state, trace_recorder=trace)
    assert update["status"] is TaskStatus.FAILED
    assert "pending_replan" not in update
    assert (await trace.list_events("boundary-task"))[0].event_type is TraceEventType.RECOVERY_RESOLVED


@pytest.mark.asyncio
async def test_human_reject_is_traced_without_immediate_replan(monkeypatch) -> None:
    state = boundary_state()
    decision = PolicyDecision(
        call_id="call-1",
        step_id=1,
        tool_name="boundary",
        risk_level=RiskLevel.HIGH,
        outcome=PolicyOutcome.REQUIRE_APPROVAL,
        reason="write capable",
        tool_source="local",
    )
    state["pending_action"] = PendingToolAction(
        call_id="call-1",
        step_id=1,
        tool_name="boundary",
        arguments={"value": 7},
        policy_decision=decision,
    )
    trace = InMemoryTraceRecorder()
    prepared = await prepare_approval_traced(state, trace_recorder=trace)
    assert prepared["status"] is TaskStatus.WAITING_APPROVAL
    monkeypatch.setattr(
        "taskpilot.graph.interrupt",
        lambda payload: {"decision": "reject", "reason": "choose another action"},
    )
    update = await human_approval_traced(state, trace_recorder=trace)
    assert update["approval_records"][-1].decision.value == "reject"
    assert "pending_replan" not in update
    assert [event.event_type for event in await trace.list_events("boundary-task")] == [
        TraceEventType.APPROVAL_REQUESTED,
        TraceEventType.APPROVAL_RESOLVED,
    ]


@pytest.mark.asyncio
async def test_tool_failure_has_terminal_trace() -> None:
    state = boundary_state()
    tool = BoundaryTool(replay_safe=True, fail=True)
    trace = InMemoryTraceRecorder()
    update = await execute_tool_action(
        state,
        executor=make_executor(tool),
        journal=InMemoryActionJournal(),
        fault_injector=NoOpExecutionFaultInjector(),
        trace_recorder=trace,
    )
    types = [event.event_type for event in await trace.list_events("boundary-task")]
    assert tool.calls == 1
    assert update["tool_calls"][0].status.value == "failed"
    assert types == [TraceEventType.TOOL_STARTED, TraceEventType.TOOL_FAILED]
