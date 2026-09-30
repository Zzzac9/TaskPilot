"""Phase 9 action boundary、crash window、recovery 与 durable HITL 测试。"""

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Sequence, cast

import pytest
import aiosqlite
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.types import Command

from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.cli import async_main
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
from taskpilot.persistence import (
    ExecutionFaultPoint,
    InjectedCrash,
    JournalStatus,
    PersistenceConfig,
    SQLiteActionJournal,
    SQLiteCheckpointProvider,
)
from taskpilot.policy import DefaultRiskPolicy
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry


class FakeFunctionCallingModel:
    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.invocations = 0
        self.messages: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any):
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations += 1
        self.messages.append(list(messages))
        return self.responses.pop(0)


def tool_call(call_id: str = "tool-1", value: int = 7) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "counter",
            "args": {"value": value},
            "id": call_id,
            "type": "tool_call",
        }],
    )


def finish_call(call_id: str = "finish-2") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": FINISH_STEP_NAME,
            "args": {
                "summary": "durable observation available",
                "evidence": ["counter observation is journaled"],
                "output": {"done": True},
            },
            "id": call_id,
            "type": "tool_call",
        }],
    )


@dataclass
class CountingTool:
    replay_safe: bool
    risk: str = "low"
    calls: list[dict[str, Any]] = field(default_factory=list)
    name: str = "counter"
    description: str = "Count deterministic offline invocations"
    input_schema: dict[str, Any] = field(default_factory=lambda: {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
        "additionalProperties": False,
    })

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "source": "local",
            "risk": self.risk,
            "replay_safe": self.replay_safe,
        }

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        return {"call_count": len(self.calls), "value": arguments["value"]}


@dataclass
class FileCounterTool(CountingTool):
    """用临时文件模拟跨 runtime 可观察的外部副作用。"""

    counter_path: Path = Path("counter.txt")
    fail_after_increment: bool = False

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        count = (
            int(self.counter_path.read_text(encoding="utf-8"))
            if self.counter_path.exists()
            else 0
        ) + 1
        self.counter_path.write_text(str(count), encoding="utf-8")
        if self.fail_after_increment:
            raise RuntimeError("controlled terminal failure")
        return {"call_count": count, "value": arguments["value"]}


class FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(goal=user_input, completion_criteria=["finish"])


class FakePlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(steps=[PlanStep(
            id=1,
            description="run one durable action",
            success_criteria=["observation exists"],
        )])


class TwoStepPlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(steps=[
            PlanStep(id=1, description="first", success_criteria=["first done"]),
            PlanStep(
                id=2,
                description="second",
                depends_on=[1],
                success_criteria=["second done"],
            ),
        ])


class FakeVerifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        return StepVerificationResult(
            step_id=kwargs["step"].id,
            verified=True,
            checks=[CriterionCheck(
                criterion_index=0,
                satisfied=True,
                reason="offline accepted",
            )],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(completed=True, reason="offline complete")


class RejectOnceVerifier(FakeVerifier):
    def __init__(self) -> None:
        self.calls = 0

    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        self.calls += 1
        if self.calls == 1:
            return StepVerificationResult(
                step_id=kwargs["step"].id,
                verified=False,
                checks=[CriterionCheck(
                    criterion_index=0,
                    satisfied=False,
                    reason="need durable evidence",
                )],
                feedback="need durable evidence",
            )
        return super().verify_step(**kwargs)


class CrashOnce:
    def __init__(self, point: ExecutionFaultPoint) -> None:
        self.point = point
        self.fired = False

    def hit(self, point: ExecutionFaultPoint, action_key: str) -> None:
        if point is self.point and not self.fired:
            self.fired = True
            raise InjectedCrash(f"{point.value}:{action_key}")


class CrashOnStepTwo(CrashOnce):
    def hit(self, point: ExecutionFaultPoint, action_key: str) -> None:
        if ":2:" in action_key:
            super().hit(point, action_key)


def initial_state(task_id: str) -> TaskState:
    return {
        "task_id": task_id,
        "user_input": "run durable fixture",
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


def make_executor(tool: CountingTool, responses: Sequence[AIMessage]) -> TaskExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return TaskExecutor(
        registry=registry,
        model=cast(BaseChatModel, FakeFunctionCallingModel(responses)),
        policy=DefaultRiskPolicy(),
    )


def graph_config(task_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": task_id},
        "recursion_limit": 100,
    }


def interrupts(snapshot: Any) -> list[Any]:
    return [item for task in snapshot.tasks for item in task.interrupts]


@pytest.mark.asyncio
async def test_action_level_graph_runs_one_tool_then_one_finish(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    tool = CountingTool(replay_safe=True)
    model = FakeFunctionCallingModel([tool_call(), finish_call()])
    registry = ToolRegistry()
    registry.register(tool)
    executor = TaskExecutor(
        registry,
        model=cast(BaseChatModel, model),
        policy=DefaultRiskPolicy(),
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(), executor=executor,
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        result = await graph.ainvoke(initial_state("action-loop"), config=graph_config("action-loop"))

    nodes = set(graph.get_graph().nodes)
    assert {"decide_action", "execute_tool_action", "build_step_outcome"} <= nodes
    assert model.invocations == 2
    assert len(tool.calls) == 1
    assert len(result["tool_calls"]) == 1
    assert result["status"] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_risk_level_and_replay_safe_are_independent() -> None:
    tool = CountingTool(replay_safe=True, risk="high")
    executor = make_executor(tool, [finish_call()])
    decision_result = await executor.decide_action(
        task_spec=TaskSpec(goal="independence"),
        step=PlanStep(id=1, description="check", success_criteria=["checked"]),
        action_index=1,
        task_results={},
        tool_calls=[],
    )
    # 上面的模型返回 finish，因此另造 frozen Tool action 更直接验证两条轴。
    from taskpilot.models import ExecutorAction, ExecutorActionType

    action_model = ExecutorAction(
        step_id=1,
        action_index=1,
        call_id="independent",
        action_type=ExecutorActionType.TOOL,
        tool_name="counter",
        arguments={"value": 7},
    )
    policy_decision = await executor.assess_action(action_model)

    assert decision_result.action is not None
    assert executor.is_replay_safe(action_model) is True
    assert policy_decision is not None
    assert policy_decision.outcome.value == "require_approval"


@pytest.mark.asyncio
async def test_terminal_journal_deduplicates_after_graph_checkpoint_crash(
    tmp_path: Path,
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "terminal-dedup"
    counter_path = tmp_path / "terminal-counter.txt"
    tool = FileCounterTool(replay_safe=False, counter_path=counter_path)
    rebuilt_tool = FileCounterTool(replay_safe=False, counter_path=counter_path)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(
                ExecutionFaultPoint.AFTER_TERMINAL_JOURNAL_BEFORE_GRAPH_RETURN
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        record = await journal.get(f"{task_id}:1:0:1")
        assert record is not None and record.status is JournalStatus.SUCCEEDED
        assert len(tool.calls) == 1

    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(rebuilt_tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))

    assert len(tool.calls) == 1
    assert rebuilt_tool.calls == []
    assert counter_path.read_text(encoding="utf-8") == "1"
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0].result == {"call_count": 1, "value": 7}
    assert result["status"] is TaskStatus.COMPLETED
    print("TERMINAL_JOURNAL_CRASH_SMOKE=" + json.dumps({
        "tool_counter_before": 0,
        "tool_counter_after_execution": 1,
        "injected_crash": True,
        "journal_status": "succeeded",
        "tool_reinvoked": False,
        "tool_counter_final": 1,
    }, sort_keys=True))


@pytest.mark.asyncio
async def test_replay_safe_started_is_automatically_retried(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "safe-started"
    tool = CountingTool(replay_safe=True)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(ExecutionFaultPoint.AFTER_JOURNAL_STARTED),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        assert tool.calls == []

    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))
        record = await journal.get(f"{task_id}:1:0:1")

    assert len(tool.calls) == 1
    assert record is not None and record.attempt_count == 2
    assert result["status"] is TaskStatus.COMPLETED
    print("REPLAY_SAFE_RECOVERY_SMOKE=" + json.dumps({
        "journal_status_before": "started",
        "automatic_retry": True,
        "recovery_interrupt": False,
        "attempt_count": record.attempt_count,
        "tool_counter_final": len(tool.calls),
    }, sort_keys=True))


@pytest.mark.asyncio
async def test_unsafe_started_interrupts_and_abort_does_not_replay(
    tmp_path: Path,
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "unsafe-abort"
    tool = CountingTool(replay_safe=False)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(
                ExecutionFaultPoint.AFTER_TOOL_RETURN_BEFORE_TERMINAL_JOURNAL
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        assert len(tool.calls) == 1

    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        paused = await graph.ainvoke(None, config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        pending_interrupts = interrupts(snapshot)
        assert paused["status"] is TaskStatus.INTERRUPTED
        assert pending_interrupts[0].value["type"] == "ambiguous_tool_recovery"
        assert len(tool.calls) == 1
        print("AMBIGUOUS_UNSAFE_CRASH_SMOKE=" + json.dumps({
            "journal": "started",
            "side_effect_counter": 1,
            "replay_safe": False,
            "automatic_retry": False,
            "recovery_interrupt": True,
        }, sort_keys=True))
        result = await graph.ainvoke(
            Command(resume={"choice": "abort", "reason": "do not duplicate"}),
            config=graph_config(task_id),
        )

    assert len(tool.calls) == 1
    assert result["status"] is TaskStatus.FAILED
    assert result["recovery_issue"] is not None


@pytest.mark.asyncio
async def test_unsafe_started_human_retry_replays_exact_action(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "unsafe-retry"
    tool = CountingTool(replay_safe=False)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(
                ExecutionFaultPoint.AFTER_TOOL_RETURN_BEFORE_TERMINAL_JOURNAL
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))

    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        await graph.ainvoke(None, config=graph_config(task_id))
        result = await graph.ainvoke(
            Command(resume={"choice": "retry", "reason": "duplicate accepted"}),
            config=graph_config(task_id),
        )
        record = await journal.get(f"{task_id}:1:0:1")

    assert len(tool.calls) == 2
    assert tool.calls[0] == tool.calls[1] == {"value": 7}
    assert record is not None and record.attempt_count == 2
    assert result["status"] is TaskStatus.COMPLETED


async def _create_second_ambiguous_crash(
    tmp_path: Path,
    task_id: str,
) -> tuple[PersistenceConfig, Path]:
    """两次真实副作用都发生，但两次 terminal journal 前均 crash。"""

    config = PersistenceConfig.from_state_dir(tmp_path)
    counter_path = tmp_path / "one-shot-counter.txt"
    first_tool = FileCounterTool(
        replay_safe=False,
        counter_path=counter_path,
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(),
            planner=FakePlanner(),
            executor=make_executor(first_tool, [tool_call()]),
            verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer,
            action_journal=journal,
            fault_injector=CrashOnce(
                ExecutionFaultPoint.AFTER_TOOL_RETURN_BEFORE_TERMINAL_JOURNAL
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
    assert counter_path.read_text(encoding="utf-8") == "1"

    second_tool = FileCounterTool(
        replay_safe=False,
        counter_path=counter_path,
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(),
            planner=FakePlanner(),
            executor=make_executor(second_tool, [finish_call("after-second-retry")]),
            verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer,
            action_journal=journal,
            fault_injector=CrashOnce(
                ExecutionFaultPoint.AFTER_TOOL_RETURN_BEFORE_TERMINAL_JOURNAL
            ),
        )
        await graph.ainvoke(None, config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        assert interrupts(snapshot)[0].value["type"] == "ambiguous_tool_recovery"
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(
                Command(resume={"choice": "retry", "reason": "one retry only"}),
                config=graph_config(task_id),
            )
        record = await journal.get(f"{task_id}:1:0:1")
        assert record is not None
        assert record.attempt_count == 2
        assert record.authorized_retry_attempt is None
    assert counter_path.read_text(encoding="utf-8") == "2"
    return config, counter_path


@pytest.mark.asyncio
async def test_unsafe_human_retry_is_one_shot_after_second_crash(
    tmp_path: Path,
) -> None:
    task_id = "unsafe-one-shot-abort"
    config, counter_path = await _create_second_ambiguous_crash(tmp_path, task_id)
    third_tool = FileCounterTool(replay_safe=False, counter_path=counter_path)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(),
            planner=FakePlanner(),
            executor=make_executor(third_tool, [finish_call("must-not-auto-run")]),
            verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        paused = await graph.ainvoke(None, config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        assert paused["status"] is TaskStatus.INTERRUPTED
        assert interrupts(snapshot)[0].value["type"] == "ambiguous_tool_recovery"
        assert counter_path.read_text(encoding="utf-8") == "2"
        assert third_tool.calls == []
        result = await graph.ainvoke(
            Command(resume={"choice": "abort", "reason": "no third side effect"}),
            config=graph_config(task_id),
        )

    assert result["status"] is TaskStatus.FAILED
    assert counter_path.read_text(encoding="utf-8") == "2"
    print(
        "ONE_SHOT_RECOVERY_SMOKE="
        + json.dumps(
            {
                "counter_after_first_ambiguous": 1,
                "counter_after_human_retry": 2,
                "second_crash": True,
                "automatic_third_invoke": False,
                "second_recovery_interrupt": True,
            },
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
async def test_second_unsafe_retry_requires_second_human_confirmation(
    tmp_path: Path,
) -> None:
    task_id = "unsafe-one-shot-retry-again"
    config, counter_path = await _create_second_ambiguous_crash(tmp_path, task_id)
    third_tool = FileCounterTool(replay_safe=False, counter_path=counter_path)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(),
            planner=FakePlanner(),
            executor=make_executor(third_tool, [finish_call("after-third-retry")]),
            verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        await graph.ainvoke(None, config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        assert interrupts(snapshot)[0].value["type"] == "ambiguous_tool_recovery"
        result = await graph.ainvoke(
            Command(resume={"choice": "retry", "reason": "second explicit retry"}),
            config=graph_config(task_id),
        )
        record = await journal.get(f"{task_id}:1:0:1")

    assert result["status"] is TaskStatus.COMPLETED
    assert counter_path.read_text(encoding="utf-8") == "3"
    assert third_tool.calls == [{"value": 7}]
    assert record is not None and record.attempt_count == 3
    assert record.authorized_retry_attempt is None


@pytest.mark.asyncio
async def test_high_risk_approval_survives_checkpointer_recreation(
    tmp_path: Path,
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "durable-approval"
    first_tool = CountingTool(replay_safe=False, risk="high")
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(first_tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        paused = await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        assert paused["status"] is TaskStatus.WAITING_APPROVAL
        assert interrupts(snapshot)[0].value["type"] == "tool_approval"
        assert first_tool.calls == []

    rebuilt_tool = CountingTool(replay_safe=False, risk="high")
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(rebuilt_tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        snapshot = await graph.aget_state(graph_config(task_id))
        assert interrupts(snapshot)[0].value["type"] == "tool_approval"
        result = await graph.ainvoke(
            Command(resume={"decision": "approve"}),
            config=graph_config(task_id),
        )

    assert rebuilt_tool.calls == [{"value": 7}]
    assert result["status"] is TaskStatus.COMPLETED
    print("DURABLE_APPROVAL_SMOKE=" + json.dumps({
        "runtime_recreated": True,
        "pending_approval_restored": True,
        "tool_counter_before_approval": 0,
        "tool_counter_after_approval": len(rebuilt_tool.calls),
        "status": result["status"].value,
    }, sort_keys=True))


@pytest.mark.asyncio
async def test_terminal_failed_journal_is_materialized_without_reinvoke(
    tmp_path: Path,
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "terminal-failed"
    counter_path = tmp_path / "failed-counter.txt"
    first_tool = FileCounterTool(
        replay_safe=False,
        counter_path=counter_path,
        fail_after_increment=True,
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(first_tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(
                ExecutionFaultPoint.AFTER_TERMINAL_JOURNAL_BEFORE_GRAPH_RETURN
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        record = await journal.get(f"{task_id}:1:0:1")
        assert record is not None and record.status is JournalStatus.FAILED

    rebuilt_tool = FileCounterTool(
        replay_safe=False,
        counter_path=counter_path,
        fail_after_increment=True,
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(rebuilt_tool, [finish_call()]),
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))

    assert counter_path.read_text(encoding="utf-8") == "1"
    assert rebuilt_tool.calls == []
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0].status.value == "failed"
    assert result["status"] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_unsafe_crash_before_invoke_is_still_ambiguous(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "unsafe-before-invoke"
    tool = CountingTool(replay_safe=False)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(ExecutionFaultPoint.AFTER_JOURNAL_STARTED),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        assert tool.calls == []

    rebuilt_tool = CountingTool(replay_safe=False)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(rebuilt_tool, [finish_call()]),
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        paused = await graph.ainvoke(None, config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))

    assert rebuilt_tool.calls == []
    assert paused["status"] is TaskStatus.INTERRUPTED
    assert interrupts(snapshot)[0].value["type"] == "ambiguous_tool_recovery"


@pytest.mark.asyncio
async def test_arguments_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "hash-mismatch"
    tool = CountingTool(replay_safe=True)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(ExecutionFaultPoint.AFTER_JOURNAL_STARTED),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))

    async with aiosqlite.connect(config.action_journal_db_path) as connection:
        await connection.execute(
            "UPDATE action_journal SET arguments_hash = ? WHERE action_key = ?",
            ("tampered", f"{task_id}:1:0:1"),
        )
        await connection.commit()

    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))

    assert tool.calls == []
    assert result["status"] is TaskStatus.FAILED
    assert "identity/hash" in result["error"]


@pytest.mark.asyncio
async def test_completed_step_is_not_rerun_after_step_two_crash(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "two-step-restart"
    counter_path = tmp_path / "two-step-counter.txt"
    first_runtime_tool = FileCounterTool(
        replay_safe=True, counter_path=counter_path
    )
    first_model = FakeFunctionCallingModel([
        tool_call("step-1-tool", value=1),
        finish_call("step-1-finish"),
        tool_call("step-2-tool", value=2),
    ])
    registry = ToolRegistry()
    registry.register(first_runtime_tool)
    first_executor = TaskExecutor(
        registry,
        model=cast(BaseChatModel, first_model),
        policy=DefaultRiskPolicy(),
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=TwoStepPlanner(),
            executor=first_executor, verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnStepTwo(
                ExecutionFaultPoint.AFTER_JOURNAL_STARTED
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        assert snapshot.values["plan"][0].status.value == "completed"
        assert counter_path.read_text(encoding="utf-8") == "1"

    rebuilt_tool = FileCounterTool(replay_safe=True, counter_path=counter_path)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=TwoStepPlanner(),
            executor=make_executor(rebuilt_tool, [finish_call("step-2-finish")]),
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))

    assert counter_path.read_text(encoding="utf-8") == "2"
    assert rebuilt_tool.calls == [{"value": 2}]
    assert [step.status.value for step in result["plan"]] == ["completed", "completed"]
    assert result["status"] is TaskStatus.COMPLETED
    print("FULL_RESTART_SMOKE=" + json.dumps({
        "step_1_tool_count": 1,
        "step_1_status": result["plan"][0].status.value,
        "step_2_tool_count": len(rebuilt_tool.calls),
        "step_2_status": result["plan"][1].status.value,
        "task_status": result["status"].value,
    }, sort_keys=True))


@pytest.mark.asyncio
async def test_verifier_retry_count_and_feedback_survive_restart(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "verifier-retry"
    tool = CountingTool(replay_safe=True)
    first_model = FakeFunctionCallingModel([
        finish_call("first-claim"),
        tool_call("retry-tool"),
    ])
    registry = ToolRegistry()
    registry.register(tool)
    first_executor = TaskExecutor(
        registry,
        model=cast(BaseChatModel, first_model),
        policy=DefaultRiskPolicy(),
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=first_executor, verifier=RejectOnceVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(ExecutionFaultPoint.AFTER_JOURNAL_STARTED),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        assert snapshot.values["plan"][0].retry_count == 1
        assert snapshot.values["step_verification"].feedback == "need durable evidence"

    second_model = FakeFunctionCallingModel([finish_call("retry-finish")])
    registry = ToolRegistry()
    registry.register(tool)
    second_executor = TaskExecutor(
        registry,
        model=cast(BaseChatModel, second_model),
        policy=DefaultRiskPolicy(),
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(), executor=second_executor,
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))

    assert result["plan"][0].retry_count == 1
    assert "need durable evidence" in str(second_model.messages[0][-1].content)
    assert result["status"] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_action_level_max_actions_remains_bounded(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    tool = CountingTool(replay_safe=True)
    registry = ToolRegistry()
    registry.register(tool)
    model = FakeFunctionCallingModel([tool_call()])
    executor = TaskExecutor(
        registry,
        model=cast(BaseChatModel, model),
        max_actions_per_step=1,
        policy=DefaultRiskPolicy(),
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(), executor=executor,
            verifier=FakeVerifier(), max_step_attempts=1,
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        result = await graph.ainvoke(
            initial_state("bounded-actions"), config=graph_config("bounded-actions")
        )

    assert model.invocations == 1
    assert len(tool.calls) == 1
    assert result["step_outcome"].error == "Executor action limit reached: 1"
    assert result["status"] is TaskStatus.FAILED


@pytest.mark.asyncio
async def test_cli_resume_completed_task_does_not_run_graph_again(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "completed-cli"
    tool = CountingTool(replay_safe=True)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call(), finish_call()]),
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        completed = await graph.ainvoke(
            initial_state(task_id), config=graph_config(task_id)
        )
        assert completed["status"] is TaskStatus.COMPLETED

    exit_code = await async_main(
        ["--resume", task_id, "--state-dir", str(tmp_path)]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "task already completed" in output
    assert "status=completed" in output
    assert f"task_id={task_id}" in output
    assert "final_output:" in output
    assert '"done": true' in output
    assert len(tool.calls) == 1
