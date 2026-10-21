"""Phase 14：Requirement Contract / 全局预算 / 有界重规划 / 可信降级基准。

场景不仅检查输出包含关键词，还检查状态路由、调用计数、来源溯源、
已验证产物、未授权副作用以及失败类型的严格 Oracle。
"""

from dataclasses import dataclass, field
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage

from taskpilot.context import ContextBuilder
from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.failure_analysis import FailureCategory, diagnose_step_failure
from taskpilot.graph import (
    decide_action,
    finalize_terminal,
    verify_step,
    build_graph,
)
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    ReplanDecision,
    ReplanProposal,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    VerificationResult,
)
from taskpilot.planner import validate_plan
from taskpilot.requirements import (
    RequirementPriority,
    build_requirement_contract,
    validate_step_references,
)
from taskpilot.service import create_initial_state
from taskpilot.tools import ToolRegistry
from taskpilot.trace import NoOpTraceRecorder


@pytest.fixture
def complex_spec() -> TaskSpec:
    return TaskSpec(
        goal="跨两个来源筛选供应商并形成对照表",
        constraints={"count": 2, "max_price": 500},
        completion_criteria=[
            "有两个确实符合价位和库存要求的供应商",
            "两个供应商的报价和库存都有可核验的来源",
            "输出包含所需的对照结果",
        ],
        optional_criteria=["如有独立详情页链接则附上"],
    )


def _step(step_id: int = 1, refs: list[str] | None = None) -> PlanStep:
    return PlanStep(
        id=step_id,
        description="Cross-source check",
        success_criteria=["可核验的结果"],
        requirement_ids=refs or ["R1", "R2", "R3"],
    )


def _reject(outcome: StepExecutionOutcome) -> StepVerificationResult:
    return StepVerificationResult(
        step_id=outcome.step_id,
        verified=False,
        feedback=outcome.error or "缺少原始证据",
        checks=[CriterionCheck(criterion_index=0, satisfied=False, reason="no proof")],
    )


def test_contract_immutable_and_distinguishes_required_optional(complex_spec: TaskSpec) -> None:
    contract = build_requirement_contract(complex_spec)
    assert [r.id for r in contract.required] == ["R1", "R2", "R3"]
    assert [r.id for r in contract.optional] == ["O1"]
    assert {r.priority for r in contract.required} == {RequirementPriority.REQUIRED}
    assert {r.priority for r in contract.optional} == {RequirementPriority.OPTIONAL}
    with pytest.raises(Exception):
        contract.required[0].statement = "伪造一个更严苛的要求"


@pytest.mark.parametrize(
    "ids",
    [[], ["R4"], ["R1", "O2"], ["UNKNOWN"], ["R1", "R99"]],
)
def test_contract_rejects_untraceable_planner_requirements(
    complex_spec: TaskSpec, ids: list[str]
) -> None:
    contract = build_requirement_contract(complex_spec)
    with pytest.raises(ValueError):
        validate_step_references(ids, contract)


def test_plan_coverage_prevents_silent_loss_of_user_hard_requirements(
    complex_spec: TaskSpec,
) -> None:
    plan = TaskPlan(steps=[_step(refs=["R1", "R2", "O1"])])
    with pytest.raises(ValueError, match="未覆盖"):
        validate_plan(plan, complex_spec)
    validate_plan(
        TaskPlan(steps=[_step(1, ["R1", "R2"]), _step(2, ["R3", "O1"])]),
        complex_spec,
    )


def test_optional_detail_can_be_omitted_without_erasing_required_spec(
    complex_spec: TaskSpec,
) -> None:
    plan = TaskPlan(steps=[_step(refs=["R1", "R2", "R3"])])
    validate_plan(plan, complex_spec)
    snapshot = ContextBuilder().build_executor_context(
        task_spec=complex_spec, step=plan.steps[0],
        task_results={}, tool_calls=[],
    )
    contract = snapshot.content["requirement_contract"]
    assert len(contract["required"]) == 3
    assert len(contract["optional"]) == 1
    assert contract["optional"][0]["statement"] == "如有独立详情页链接则附上"


@pytest.mark.parametrize(
    ("error", "expected", "can_replan"),
    [
        ("Executor action limit reached: 10", FailureCategory.ACTION_LIMIT, True),
        ("Task action budget reached: 60", FailureCategory.GLOBAL_BUDGET, False),
        ("Tool call failed: timeout", FailureCategory.TOOL_ERROR, True),
        ("", FailureCategory.EVIDENCE_GAP, True),
    ],
)
def test_failure_diagnosis_is_auditable_and_never_fabricates_success(
    error: str, expected: FailureCategory, can_replan: bool
) -> None:
    outcome = StepExecutionOutcome(step_id=1, claimed_complete=not bool(error), error=error or None)
    diagnosis = diagnose_step_failure(outcome, _reject(outcome))
    assert diagnosis.category is expected
    assert diagnosis.can_replan is can_replan


def _state(
    spec: TaskSpec,
    *,
    task_id: str = "phase14-task",
    step_results: list[StepResult] | None = None,
    error: str = "Step 2 missing evidence",
) -> dict[str, Any]:
    s = create_initial_state(spec.goal, task_id=task_id)
    s.update(
        task_spec=spec,
        plan=[_step()],
        current_step_id=1,
        step_results=step_results or [],
        status=TaskStatus.FAILED,
        error=error,
    )
    return s


@pytest.mark.parametrize(
    ("with_verified", "error_prefix", "expected"),
    [
        (False, "Step 2 no evidence", TaskStatus.FAILED),
        (True, "Step 2 no evidence", TaskStatus.PARTIAL),
        (True, "Task Verifier 调用失败: ValueError", TaskStatus.FAILED),
        (True, "Human Assistance canceled by user: stop", TaskStatus.FAILED),
    ],
)
def test_partial_output_only_cites_verified_work_and_never_masks_runtime_failure(
    complex_spec: TaskSpec,
    with_verified: bool,
    error_prefix: str,
    expected: TaskStatus,
) -> None:
    verified = [StepResult(step_id=1, summary="Vendor A read and checked",
                           output={"value": 420}, evidence=["Oracle A"])] if with_verified else []
    s = _state(complex_spec, step_results=verified, error=error_prefix)
    s["step_outcome"] = StepExecutionOutcome(
        step_id=2, claimed_complete=True, summary="unverified fabricated candidate",
        output={"value": 999},
    )
    update = finalize_terminal(s)
    assert update["status"] is expected
    answer = update["final_answer"]
    assert ("已完成并通过验证的结果" in answer) is with_verified
    if with_verified:
        assert "Vendor A read and checked" in answer
    assert "未验证的部分结果（仅供参考）" in answer
    assert "unverified fabricated candidate" in answer
    assert ("## 任务部分完成" in answer) is (expected is TaskStatus.PARTIAL)


class AlwaysRejectVerifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        return _reject(kwargs["outcome"])

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(completed=False, reason="not complete")


@pytest.mark.parametrize(
    ("error", "expect_replan"),
    [
        ("Executor action limit reached: 10", True),
        ("Task action budget reached: 3", False),
    ],
)
def test_step_limit_can_replan_but_global_limit_must_not_be_renewed(
    complex_spec: TaskSpec, error: str, expect_replan: bool
) -> None:
    state = _state(complex_spec, error=error)
    state["status"] = TaskStatus.RUNNING
    state["step_outcome"] = StepExecutionOutcome(
        step_id=1, claimed_complete=False, action_count=10, error=error
    )
    update = verify_step(
        state, verifier=AlwaysRejectVerifier(), max_step_attempts=1, max_replans=2,
    )
    assert ("pending_replan" in update) is expect_replan
    assert update["status"] is (TaskStatus.RUNNING if expect_replan else TaskStatus.FAILED)
    if expect_replan:
        assert update["pending_replan"].failure_category == "action_limit"
    else:
        assert "global action budget exhausted" in update["error"]


class ScriptedModel:
    def __init__(self, messages: Sequence[AIMessage]) -> None:
        self.responses = list(messages)
        self.invocations = 0

    def bind_tools(self, schemas: Any) -> "ScriptedModel":
        return self

    async def ainvoke(self, messages: Any) -> AIMessage:
        self.invocations += 1
        return self.responses.pop(0)


@dataclass
class CountingReadTool:
    name: str = "read_fixture"
    description: str = "Read a safe fixture with no side effects"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    metadata: dict[str, Any] = field(
        default_factory=lambda: {
            "source": "local", "risk": "low",
            "replay_safe": True, "read_only": True,
        }
    )
    calls: int = 0

    async def invoke(self, args: dict[str, Any]) -> dict[str, str]:
        self.calls += 1
        return {"verified": "source-data-only"}


def tool_message(identity: str) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{
            "name": "read_fixture", "args": {},
            "id": identity, "type": "tool_call"
        }],
    )


def finish_message(identity: str) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{
            "name": FINISH_STEP_NAME,
            "args": {
                "summary": "source-data-only result",
                "evidence": ["read_fixture returned source-data-only"],
                "output": {"value": "source-data-only"},
            },
            "id": identity, "type": "tool_call",
        }],
    )


class OfflineAnalyzer:
    def analyze(self, input_text: str) -> TaskSpec:
        return TaskSpec(goal=input_text, completion_criteria=["one real read-backed value"])


class OfflinePlanner:
    def plan(self, spec: TaskSpec) -> TaskPlan:
        return TaskPlan(steps=[PlanStep(
            id=1, description="Try a read",
            success_criteria=["read back source value"],
            requirement_ids=["R1"],
        )])


class OfflineReplanner:
    def __init__(self):
        self.contexts: list[dict[str, Any]] = []

    def replan(self, context: Any) -> ReplanProposal:
        self.contexts.append(context.content)
        return ReplanProposal(
            decision=ReplanDecision.REPLAN,
            reason="first bounded path timed out; reuse evidence in a new path",
            steps=[PlanStep(
                id=2, description="Alternative finish path using durable read",
                success_criteria=["value is sourced"],
                requirement_ids=["R1"],
            )],
        )


class OfflineVerifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        step = kwargs["step"]
        evidence = any(
            t.tool_name == "read_fixture" and t.result == {"verified": "source-data-only"}
            for t in kwargs["tool_calls"]
        )
        ok = kwargs["outcome"].claimed_complete and evidence
        return StepVerificationResult(
            step_id=step.id, verified=ok,
            checks=[CriterionCheck(
                criterion_index=i, satisfied=ok,
                reason="read verified" if ok else "missing true tool observation"
            ) for i, _ in enumerate(step.success_criteria)],
            feedback=None if ok else "Read actual fixture before claiming completion",
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        verified = kwargs["step_results"]
        ok = any(
            r.output.get("value") == "source-data-only" for r in verified
        )
        return VerificationResult(completed=ok, reason="external source backed" if ok else "no source")


@pytest.mark.asyncio
async def test_full_replan_retains_global_budget_and_no_false_completion() -> None:
    """实际 LangGraph 执行：读数据→达到 step limit→replan→完成；不重置累计 budget。"""
    registry = ToolRegistry()
    tool = CountingReadTool()
    registry.register(tool)
    model = ScriptedModel([tool_message("tool-1"), finish_message("finish-2")])
    replanner = OfflineReplanner()
    executor = TaskExecutor(
        registry, model=cast(BaseChatModel, model), max_actions_per_step=1,
    )
    graph = build_graph(
        analyzer=OfflineAnalyzer(), planner=OfflinePlanner(),
        executor=executor, verifier=OfflineVerifier(),
        replanner=replanner, max_step_attempts=1,
        max_total_actions=2, max_replans=1,
    )
    result = await graph.ainvoke(create_initial_state("Read a value"))
    assert result["status"] is TaskStatus.COMPLETED
    assert result["total_action_decisions"] == 2
    assert len(result["tool_calls"]) == 1
    assert tool.calls == 1
    assert model.invocations == 2
    assert result["replan_count"] == 1
    assert replanner.contexts[0]["failure_category"] == "action_limit"
    assert len(result["step_results"]) == 1
    assert result["step_results"][0].output["value"] == "source-data-only"


@pytest.mark.asyncio
async def test_global_budget_blocks_replan_from_buying_more_tool_calls() -> None:
    registry = ToolRegistry()
    tool = CountingReadTool()
    registry.register(tool)
    model = ScriptedModel([tool_message("tool-1")])
    replanner = OfflineReplanner()
    executor = TaskExecutor(
        registry, model=cast(BaseChatModel, model), max_actions_per_step=1,
    )
    graph = build_graph(
        analyzer=OfflineAnalyzer(), planner=OfflinePlanner(),
        executor=executor, verifier=OfflineVerifier(),
        replanner=replanner, max_step_attempts=1,
        max_total_actions=1, max_replans=2,
    )
    result = await graph.ainvoke(create_initial_state("Read a value"))
    assert result["status"] is TaskStatus.FAILED
    assert result["total_action_decisions"] == 1
    assert model.invocations == 1
    assert tool.calls == 1
    assert not replanner.contexts
    assert "global action budget exhausted" in result["error"]


@pytest.mark.asyncio
async def test_durable_counter_does_not_reset_on_new_step_attempt() -> None:
    spec = TaskSpec(goal="fact", completion_criteria=["fact verified"])
    state = _state(spec)
    state["status"] = TaskStatus.RUNNING
    state["total_action_decisions"] = 3
    model = ScriptedModel([])
    registry = ToolRegistry()
    registry.register(CountingReadTool())
    executor = TaskExecutor(registry, model=cast(BaseChatModel, model))
    update = await decide_action(
        state, executor=executor, trace_recorder=NoOpTraceRecorder(),
        max_total_actions=3,
    )
    assert update["step_outcome"].error == "Task action budget reached: 3"
    assert model.invocations == 0
    assert state["total_action_decisions"] == 3
