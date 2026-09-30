"""Phase 4 Executor Function Calling Runtime 的离线测试。"""

import json
from dataclasses import dataclass, field
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from taskpilot.executor import (
    EXECUTOR_SYSTEM_PROMPT,
    FINISH_STEP_NAME,
    ExecutorRunResult,
    TaskExecutor,
    build_executor_messages,
)
from taskpilot.graph import build_graph
from taskpilot.models import (
    PlanStep,
    PlanStepStatus,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    CriterionCheck,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    ToolCallRecord,
    ToolCallStatus,
    VerificationResult,
)
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry


LOOKUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


@dataclass
class LookupTool:
    name: str = "lookup"
    description: str = "Return fixed offline candidates."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: dict(LOOKUP_SCHEMA)
    )

    async def invoke(self, arguments: dict[str, Any]) -> Any:
        return {
            "query": arguments["query"],
            "items": ["candidate-a", "candidate-b"],
        }


@dataclass
class FailingTool:
    name: str = "lookup"
    description: str = "Fail in a controlled offline manner."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: dict(LOOKUP_SCHEMA)
    )

    async def invoke(self, arguments: dict[str, Any]) -> Any:
        raise RuntimeError("controlled lookup failure")


class FakeFunctionCallingModel:
    """按顺序返回 AIMessage，并保留每轮完整消息快照。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.bound_tools: list[dict[str, Any]] = []
        self.invocations: list[list[BaseMessage]] = []

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> "FakeFunctionCallingModel":
        self.bound_tools = list(tools)
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations.append(list(messages))
        return self.responses.pop(0)


def tool_call(
    name: str,
    arguments: dict[str, Any],
    call_id: str,
) -> AIMessage:
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


def finish_call(call_id: str = "finish-1") -> AIMessage:
    return tool_call(
        FINISH_STEP_NAME,
        {
            "summary": "Current step has enough offline evidence.",
            "evidence": ["Two fixed candidates were returned."],
            "output": {"items": ["candidate-a", "candidate-b"]},
        },
        call_id,
    )


def make_task_spec() -> TaskSpec:
    return TaskSpec(
        goal="Find offline candidates",
        constraints={"count": 2},
        completion_criteria=["Return two candidates"],
    )


def make_step(step_id: int = 1) -> PlanStep:
    return PlanStep(
        id=step_id,
        description="Discover candidates that can be verified",
        success_criteria=["At least two candidates are available"],
    )


def make_registry(tool: LookupTool | FailingTool | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    if tool is not None:
        registry.register(tool)
    return registry


@pytest.mark.asyncio
async def test_executor_runs_tool_then_finish_step_with_linked_tool_message() -> None:
    model = FakeFunctionCallingModel(
        [
            tool_call("lookup", {"query": "gym"}, "lookup-1"),
            finish_call("finish-2"),
        ]
    )
    step = make_step()
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, model),
    )

    result = await executor.execute(
        task_spec=make_task_spec(),
        step=step,
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is True
    assert result.outcome.action_count == 2
    assert result.outcome.evidence == ["Two fixed candidates were returned."]
    assert result.outcome.output == {
        "items": ["candidate-a", "candidate-b"]
    }
    assert result.fatal_error is False
    assert len(result.tool_calls) == 1
    record = result.tool_calls[0]
    assert record.call_id == "lookup-1"
    assert record.step_id == step.id
    assert record.status is ToolCallStatus.SUCCESS
    assert record.result == {
        "query": "gym",
        "items": ["candidate-a", "candidate-b"],
    }
    assert step.status is PlanStepStatus.PENDING

    second_round_messages = model.invocations[1]
    assert isinstance(second_round_messages[-2], AIMessage)
    assert isinstance(second_round_messages[-1], ToolMessage)
    assert second_round_messages[-2].tool_calls[0]["id"] == "lookup-1"
    assert second_round_messages[-1].tool_call_id == "lookup-1"
    assert second_round_messages[-1].status == "success"

    schema_names = [schema["function"]["name"] for schema in model.bound_tools]
    assert schema_names == ["lookup", FINISH_STEP_NAME]


@pytest.mark.asyncio
async def test_tool_failure_becomes_observation_and_loop_can_continue() -> None:
    model = FakeFunctionCallingModel(
        [
            tool_call("lookup", {"query": "gym"}, "failed-1"),
            tool_call("backup_lookup", {"query": "gym"}, "backup-2"),
            finish_call("finish-after-error"),
        ]
    )
    registry = make_registry(FailingTool())
    registry.register(LookupTool(name="backup_lookup"))
    executor = TaskExecutor(
        registry=registry,
        model=cast(BaseChatModel, model),
    )

    result = await executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is True
    assert result.outcome.action_count == 3
    assert result.fatal_error is False
    assert result.tool_calls[0].status is ToolCallStatus.FAILED
    assert result.tool_calls[1].status is ToolCallStatus.SUCCESS
    assert "controlled lookup failure" in result.tool_calls[0].error
    error_message = model.invocations[1][-1]
    assert isinstance(error_message, ToolMessage)
    assert error_message.tool_call_id == "failed-1"
    assert error_message.status == "error"
    assert "controlled lookup failure" in str(error_message.content)


@pytest.mark.asyncio
async def test_tool_error_does_not_make_graph_task_failed() -> None:
    model = FakeFunctionCallingModel(
        [
            tool_call("lookup", {"query": "gym"}, "graph-failed-1"),
            tool_call("backup_lookup", {"query": "gym"}, "graph-backup-2"),
            finish_call("graph-finish-3"),
        ]
    )
    registry = make_registry(FailingTool())
    registry.register(LookupTool(name="backup_lookup"))
    executor = TaskExecutor(
        registry=registry,
        model=cast(BaseChatModel, model),
    )

    result = await build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor,
        verifier=FakeVerifier(),
    ).ainvoke(make_initial_state())

    assert result["status"] is TaskStatus.COMPLETED
    assert result["tool_calls"][0].status is ToolCallStatus.FAILED
    assert result["tool_calls"][1].status is ToolCallStatus.SUCCESS
    assert result["step_outcome"] is None
    assert result["step_results"][0].summary == (
        "Current step has enough offline evidence."
    )
    assert result["plan"][0].status is PlanStepStatus.COMPLETED
    assert result["error"] is None


@pytest.mark.asyncio
async def test_action_limit_returns_incomplete_outcome() -> None:
    model = FakeFunctionCallingModel(
        [tool_call("lookup", {"query": "gym"}, "limit-1")]
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, model),
        max_actions_per_step=1,
    )

    result = await executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is False
    assert result.outcome.action_count == 1
    assert result.outcome.error == "Executor action limit reached: 1"
    assert len(result.tool_calls) == 1
    assert result.fatal_error is False


@pytest.mark.asyncio
async def test_multiple_tool_calls_are_rejected() -> None:
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {"query": "one"},
                "id": "multi-1",
                "type": "tool_call",
            },
            {
                "name": "lookup",
                "args": {"query": "two"},
                "id": "multi-2",
                "type": "tool_call",
            },
        ],
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, FakeFunctionCallingModel([response])),
    )

    result = await executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.fatal_error is True
    assert "实际数量: 2" in result.outcome.error
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_unknown_tool_call_returns_clear_error() -> None:
    model = FakeFunctionCallingModel(
        [tool_call("missing", {"query": "gym"}, "unknown-1")]
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, model),
    )

    result = await executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.fatal_error is True
    assert result.outcome.error == "未注册的工具: missing"
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_invalid_tool_arguments_return_clear_error() -> None:
    model = FakeFunctionCallingModel(
        [tool_call("lookup", {}, "invalid-1")]
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, model),
    )

    result = await executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.fatal_error is True
    assert "参数不符合 input_schema" in result.outcome.error
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_missing_action_is_not_treated_as_completion() -> None:
    model = FakeFunctionCallingModel([AIMessage(content="I am done")])
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, model),
    )

    result = await executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is False
    assert result.fatal_error is True
    assert "实际数量: 0" in result.outcome.error


@pytest.mark.asyncio
async def test_finish_step_requires_nonempty_evidence() -> None:
    response = tool_call(
        FINISH_STEP_NAME,
        {"summary": "Done", "evidence": [], "output": {}},
        "empty-evidence-1",
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, FakeFunctionCallingModel([response])),
    )

    result = await executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is False
    assert result.fatal_error is True
    assert "finish_step 参数无效" in result.outcome.error


@pytest.mark.asyncio
async def test_finish_step_requires_structured_output_object() -> None:
    response = tool_call(
        FINISH_STEP_NAME,
        {"summary": "Done", "evidence": ["Evidence"]},
        "missing-output-1",
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, FakeFunctionCallingModel([response])),
    )

    result = await executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is False
    assert result.fatal_error is True
    assert "output" in result.outcome.error


@pytest.mark.asyncio
async def test_empty_registry_reports_execution_unavailable_without_model_call() -> None:
    model = FakeFunctionCallingModel([])
    executor = TaskExecutor(
        registry=make_registry(),
        model=cast(BaseChatModel, model),
    )

    result = await executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is False
    assert result.outcome.action_count == 0
    assert result.outcome.error == "Executor 当前没有注册任何外部工具"
    assert result.fatal_error is True
    assert model.invocations == []


def test_context_contains_only_relevant_structured_state() -> None:
    messages = build_executor_messages(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={"selected": ["candidate-a"]},
        tool_calls=[
            ToolCallRecord(
                call_id="current-1",
                step_id=1,
                tool_name="lookup",
                arguments={"query": "gym"},
                status=ToolCallStatus.SUCCESS,
                result={"items": ["candidate-a"]},
            ),
            ToolCallRecord(
                call_id="future-1",
                step_id=2,
                tool_name="lookup",
                arguments={"query": "other"},
                status=ToolCallStatus.SUCCESS,
                result={"items": ["should-not-appear"]},
            ),
        ],
    )

    assert EXECUTOR_SYSTEM_PROMPT in str(messages[0].content)
    assert isinstance(messages[1], HumanMessage)
    context = json.loads(str(messages[1].content))
    assert context["task_goal"] == "Find offline candidates"
    assert context["current_step"]["id"] == 1
    assert context["current_step_observations"][0]["call_id"] == "current-1"
    assert len(context["current_step_observations"]) == 1
    assert "plan" not in context
    assert context["verification_feedback"] is None


def test_context_filters_dependency_results_and_feedback_by_current_step() -> None:
    step = PlanStep(
        id=2,
        description="Organize candidates",
        depends_on=[1],
        success_criteria=["Candidates are organized"],
    )
    matching_feedback = StepVerificationResult(
        step_id=2,
        verified=False,
        checks=[
            CriterionCheck(
                criterion_index=0,
                satisfied=False,
                reason="Missing names",
            )
        ],
        feedback="Add candidate names",
    )
    messages = build_executor_messages(
        task_spec=make_task_spec(),
        step=step,
        task_results={},
        tool_calls=[],
        step_results=[
            StepResult(step_id=1, summary="Dependency", output={"items": ["A"]}),
            StepResult(step_id=99, summary="Unrelated", output={"secret": True}),
        ],
        verification_feedback=matching_feedback,
    )

    context = json.loads(str(messages[1].content))
    assert [item["step_id"] for item in context["dependency_results"]] == [1]
    assert context["verification_feedback"]["feedback"] == "Add candidate names"
    assert "Unrelated" not in str(context)


def test_context_excludes_feedback_for_other_step() -> None:
    messages = build_executor_messages(
        task_spec=make_task_spec(),
        step=make_step(2),
        task_results={},
        tool_calls=[],
        verification_feedback=StepVerificationResult(
            step_id=1,
            verified=False,
            checks=[],
            feedback="Old feedback",
        ),
    )

    context = json.loads(str(messages[1].content))
    assert context["verification_feedback"] is None


def test_executor_prompt_preserves_phase_4_boundaries() -> None:
    assert "只执行当前 PlanStep" in EXECUTOR_SYSTEM_PROMPT
    assert "不执行未来步骤" in EXECUTOR_SYSTEM_PROMPT
    assert "不得虚构外部结果" in EXECUTOR_SYSTEM_PROMPT
    assert "每轮必须且最多选择一个动作" in EXECUTOR_SYSTEM_PROMPT
    assert "单次 Tool 失败不等于 Step 或 Task 失败" in EXECUTOR_SYSTEM_PROMPT
    assert "success_criteria 已有充分 evidence" in EXECUTOR_SYSTEM_PROMPT
    assert "未来 Verifier 判断" in EXECUTOR_SYSTEM_PROMPT
    assert "不要直接把 PlanStep 或整个 Task 声称为 completed" in (
        EXECUTOR_SYSTEM_PROMPT
    )


class FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return make_task_spec()


class FakePlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                make_step(1),
            ]
        )


class FakeExecutor:
    def __init__(self) -> None:
        self.executed_step_ids: list[int] = []

    async def execute(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult],
        verification_feedback: StepVerificationResult | None,
        policy_decisions: Sequence[Any],
        approval_records: Sequence[Any],
    ) -> ExecutorRunResult:
        self.executed_step_ids.append(step.id)
        return ExecutorRunResult(
            outcome=StepExecutionOutcome(
                step_id=step.id,
                claimed_complete=True,
                summary="Fake executor claim",
                evidence=["Fake evidence"],
                output={"items": ["candidate-a"]},
                action_count=2,
            ),
            tool_calls=[
                ToolCallRecord(
                    call_id="graph-call-1",
                    step_id=step.id,
                    tool_name="lookup",
                    arguments={"query": "gym"},
                    status=ToolCallStatus.SUCCESS,
                    result={"items": ["candidate-a"]},
                )
            ],
        )


class FakeVerifier:
    def verify_step(self, *, task_spec, step, outcome, tool_calls, dependency_results):
        return StepVerificationResult(
            step_id=step.id,
            verified=True,
            checks=[
                CriterionCheck(
                    criterion_index=index,
                    satisfied=True,
                    reason="Fake evidence accepted",
                )
                for index, _ in enumerate(step.success_criteria)
            ],
        )

    def verify_task(self, *, task_spec, step_results, task_results):
        return VerificationResult(completed=True, reason="Fake task accepted")


def make_initial_state() -> TaskState:
    return {
        "task_id": "phase-4-graph",
        "user_input": "Find offline candidates",
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


@pytest.mark.asyncio
async def test_graph_verifies_and_completes_single_step() -> None:
    executor = FakeExecutor()
    graph = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor,
        verifier=FakeVerifier(),
    )

    result = await graph.ainvoke(make_initial_state())

    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    assert ("plan_task", "execute_step") in edges
    assert ("execute_step", "__end__") in edges
    assert executor.executed_step_ids == [1]
    assert result["status"] is TaskStatus.COMPLETED
    assert result["current_step_id"] is None
    assert result["plan"][0].status is PlanStepStatus.COMPLETED
    assert result["step_outcome"] is None
    assert result["step_results"][0].step_id == 1
    assert result["tool_calls"][0].call_id == "graph-call-1"
    assert result["error"] is None
