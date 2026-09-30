"""Phase 5 验证、重试、依赖交接与任务完成门控的离线测试。"""

from collections import defaultdict
from typing import Any, Sequence

import pytest

from taskpilot.executor import ExecutorRunResult
from taskpilot.graph import advance_step, build_graph, find_next_executable_step
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    PlanStepStatus,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    ToolCallRecord,
    VerificationResult,
)
from taskpilot.state import TaskState


class FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(
            goal=user_input,
            constraints={"count": 2},
            completion_criteria=["返回两个整理后的离线候选"],
        )


class TwoStepPlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="发现离线候选",
                    success_criteria=["获得两个候选"],
                ),
                PlanStep(
                    id=2,
                    description="整理离线候选",
                    depends_on=[1],
                    success_criteria=["输出两个候选名称"],
                ),
            ]
        )


class OneStepPlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="生成离线结果",
                    success_criteria=["结果存在"],
                )
            ]
        )


class RecordingExecutor:
    """返回固定 Claim，并保留 Graph 传入的跨步骤上下文。"""

    def __init__(self, *, fatal: bool = False) -> None:
        self.fatal = fatal
        self.call_count_by_step: dict[int, int] = defaultdict(int)
        self.received_step_results: list[tuple[int, list[StepResult]]] = []
        self.received_feedback: list[tuple[int, StepVerificationResult | None]] = []

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
        self.call_count_by_step[step.id] += 1
        self.received_step_results.append((step.id, list(step_results)))
        self.received_feedback.append((step.id, verification_feedback))
        if self.fatal:
            return ExecutorRunResult(
                outcome=StepExecutionOutcome(
                    step_id=step.id,
                    claimed_complete=False,
                    error="fatal offline executor error",
                ),
                fatal_error=True,
            )
        output = (
            {"candidates": [{"name": "A"}, {"name": "B"}]}
            if step.id == 1
            else {"names": ["A", "B"]}
        )
        return ExecutorRunResult(
            outcome=StepExecutionOutcome(
                step_id=step.id,
                claimed_complete=True,
                summary=f"Step {step.id} offline claim",
                evidence=[f"step-{step.id}-evidence"],
                output=output,
                action_count=1,
            )
        )


class SequencedVerifier:
    """按调用顺序接受或拒绝 Step，并返回固定 Task 结论。"""

    def __init__(
        self,
        step_decisions: Sequence[bool],
        task_result: VerificationResult | None = None,
    ) -> None:
        self.step_decisions = list(step_decisions)
        self.task_result = task_result or VerificationResult(
            completed=True,
            reason="离线任务条件全部满足",
        )
        self.step_call_count = 0
        self.task_call_count = 0
        self.dependency_results_seen: list[tuple[int, list[StepResult]]] = []

    def verify_step(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        outcome: StepExecutionOutcome,
        tool_calls: Sequence[ToolCallRecord],
        dependency_results: Sequence[StepResult],
    ) -> StepVerificationResult:
        decision = self.step_decisions[self.step_call_count]
        self.step_call_count += 1
        self.dependency_results_seen.append((step.id, list(dependency_results)))
        return StepVerificationResult(
            step_id=step.id,
            verified=decision,
            checks=[
                CriterionCheck(
                    criterion_index=index,
                    satisfied=decision,
                    reason="满足" if decision else "证据不足",
                )
                for index, _ in enumerate(step.success_criteria)
            ],
            feedback=None if decision else "请补充可核验的候选证据",
        )

    def verify_task(
        self,
        *,
        task_spec: TaskSpec,
        step_results: Sequence[StepResult],
        task_results: dict[str, Any],
    ) -> VerificationResult:
        self.task_call_count += 1
        return self.task_result


def make_initial_state() -> TaskState:
    return {
        "task_id": "phase-5-offline",
        "user_input": "获取两个离线候选并整理结果",
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
async def test_multi_step_graph_hands_off_verified_result_and_completes() -> None:
    executor = RecordingExecutor()
    verifier = SequencedVerifier([True, True])

    result = await build_graph(
        analyzer=FakeAnalyzer(),
        planner=TwoStepPlanner(),
        executor=executor,
        verifier=verifier,
    ).ainvoke(make_initial_state())

    assert [step.status for step in result["plan"]] == [
        PlanStepStatus.COMPLETED,
        PlanStepStatus.COMPLETED,
    ]
    assert result["status"] is TaskStatus.COMPLETED
    assert result["current_step_id"] is None
    assert [item.step_id for item in result["step_results"]] == [1, 2]
    assert result["step_results"][0].output == {
        "candidates": [{"name": "A"}, {"name": "B"}]
    }
    step_2_input = next(
        items for step_id, items in executor.received_step_results if step_id == 2
    )
    assert [item.step_id for item in step_2_input] == [1]
    step_2_verifier_input = next(
        items for step_id, items in verifier.dependency_results_seen if step_id == 2
    )
    assert [item.step_id for item in step_2_verifier_input] == [1]
    assert verifier.task_call_count == 1
    assert result["verification"].completed is True


@pytest.mark.asyncio
async def test_rejection_retries_same_step_with_feedback_then_advances() -> None:
    executor = RecordingExecutor()
    verifier = SequencedVerifier([False, True])

    result = await build_graph(
        analyzer=FakeAnalyzer(),
        planner=OneStepPlanner(),
        executor=executor,
        verifier=verifier,
        max_step_attempts=3,
    ).ainvoke(make_initial_state())

    assert executor.call_count_by_step[1] == 2
    assert result["plan"][0].retry_count == 1
    assert result["plan"][0].status is PlanStepStatus.COMPLETED
    assert executor.received_feedback[0] == (1, None)
    retry_feedback = executor.received_feedback[1][1]
    assert retry_feedback is not None
    assert retry_feedback.verified is False
    assert retry_feedback.feedback == "请补充可核验的候选证据"
    assert result["status"] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_repeated_rejection_stops_at_retry_bound() -> None:
    executor = RecordingExecutor()
    verifier = SequencedVerifier([False, False, False, True])

    result = await build_graph(
        analyzer=FakeAnalyzer(),
        planner=OneStepPlanner(),
        executor=executor,
        verifier=verifier,
        max_step_attempts=3,
    ).ainvoke(make_initial_state())

    assert executor.call_count_by_step[1] == 3
    assert verifier.step_call_count == 3
    assert verifier.task_call_count == 0
    assert result["plan"][0].retry_count == 3
    assert result["plan"][0].status is PlanStepStatus.FAILED
    assert result["status"] is TaskStatus.FAILED
    assert "max_step_attempts=3" in result["error"]


@pytest.mark.asyncio
async def test_task_verifier_rejection_fails_task_after_steps_complete() -> None:
    verifier = SequencedVerifier(
        [True],
        task_result=VerificationResult(
            completed=False,
            reason="最终格式仍缺失",
            missing_requirements=["CSV 输出"],
            next_action="replan",
        ),
    )

    result = await build_graph(
        analyzer=FakeAnalyzer(),
        planner=OneStepPlanner(),
        executor=RecordingExecutor(),
        verifier=verifier,
    ).ainvoke(make_initial_state())

    assert result["plan"][0].status is PlanStepStatus.COMPLETED
    assert result["verification"].completed is False
    assert result["status"] is TaskStatus.FAILED
    assert "next_action=replan" in result["error"]


@pytest.mark.asyncio
async def test_inconsistent_task_verification_fails_validation() -> None:
    verifier = SequencedVerifier(
        [True],
        task_result=VerificationResult(
            completed=True,
            reason="矛盾",
            missing_requirements=["仍缺内容"],
        ),
    )

    result = await build_graph(
        analyzer=FakeAnalyzer(),
        planner=OneStepPlanner(),
        executor=RecordingExecutor(),
        verifier=verifier,
    ).ainvoke(make_initial_state())

    assert result["status"] is TaskStatus.FAILED
    assert "missing_requirements 必须为空" in result["error"]


@pytest.mark.asyncio
async def test_fatal_executor_error_bypasses_step_and_task_verifier() -> None:
    verifier = SequencedVerifier([True])

    result = await build_graph(
        analyzer=FakeAnalyzer(),
        planner=OneStepPlanner(),
        executor=RecordingExecutor(fatal=True),
        verifier=verifier,
    ).ainvoke(make_initial_state())

    assert result["status"] is TaskStatus.FAILED
    assert verifier.step_call_count == 0
    assert verifier.task_call_count == 0
    assert result["error"] == "fatal offline executor error"


def test_next_step_selection_is_dependency_aware_and_plan_ordered() -> None:
    plan = [
        PlanStep(
            id=10,
            description="Completed",
            status=PlanStepStatus.COMPLETED,
            success_criteria=["done"],
        ),
        PlanStep(
            id=30,
            description="First ready in plan order",
            depends_on=[10],
            success_criteria=["done"],
        ),
        PlanStep(
            id=20,
            description="Second ready in plan order",
            depends_on=[10],
            success_criteria=["done"],
        ),
    ]

    assert find_next_executable_step(plan) == 30


def test_blocked_pending_plan_returns_clear_error() -> None:
    plan = [
        PlanStep(
            id=2,
            description="Blocked",
            depends_on=[1],
            success_criteria=["done"],
        )
    ]

    with pytest.raises(ValueError, match="Plan 依赖阻塞"):
        find_next_executable_step(plan)


def test_advance_step_replaces_existing_result_without_duplicate() -> None:
    step = PlanStep(
        id=1,
        description="One",
        status=PlanStepStatus.RUNNING,
        success_criteria=["done"],
    )
    state = make_initial_state()
    state.update(
        {
            "task_spec": TaskSpec(goal="One"),
            "plan": [step],
            "current_step_id": 1,
            "step_outcome": StepExecutionOutcome(
                step_id=1,
                claimed_complete=True,
                summary="new summary",
                evidence=["new evidence"],
                output={"value": "new"},
            ),
            "step_results": [
                StepResult(
                    step_id=1,
                    summary="old summary",
                    output={"value": "old"},
                )
            ],
            "step_verification": StepVerificationResult(
                step_id=1,
                verified=True,
                checks=[
                    CriterionCheck(
                        criterion_index=0,
                        satisfied=True,
                        reason="verified",
                    )
                ],
            ),
            "status": TaskStatus.RUNNING,
        }
    )

    update = advance_step(state)

    assert update["plan"][0].status is PlanStepStatus.COMPLETED
    assert len(update["step_results"]) == 1
    assert update["step_results"][0].summary == "new summary"
    assert update["step_results"][0].output == {"value": "new"}
