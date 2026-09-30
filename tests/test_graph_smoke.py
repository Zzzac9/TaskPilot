"""Phase 4 状态图的离线 smoke test。"""

import pytest

from taskpilot.executor import ExecutorRunResult
from taskpilot.graph import build_graph
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    StepExecutionOutcome,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    VerificationResult,
)
from taskpilot.state import TaskState


class StubAnalyzer:
    """避免 smoke test 访问真实模型。"""

    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(
            goal=user_input,
            completion_criteria=["返回符合用户要求的结果"],
        )


class StubPlanner:
    """返回一个最小且有效的离线计划。"""

    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="Prepare the requested result",
                    success_criteria=["The requested result is prepared"],
                )
            ]
        )


class StubExecutor:
    """返回一个离线执行声明。"""

    async def execute(
        self,
        *,
        task_spec,
        step,
        task_results,
        tool_calls,
        step_results,
        verification_feedback,
        policy_decisions,
        approval_records,
    ):
        return ExecutorRunResult(
            outcome=StepExecutionOutcome(
                step_id=step.id,
                claimed_complete=True,
                summary="Prepared",
                evidence=["Stub evidence"],
                output={"prepared": True},
                action_count=1,
            )
        )


class StubVerifier:
    """离线接受步骤与任务，避免 smoke test 访问真实模型。"""

    def verify_step(self, *, task_spec, step, outcome, tool_calls, dependency_results):
        return StepVerificationResult(
            step_id=step.id,
            verified=True,
            checks=[
                CriterionCheck(
                    criterion_index=index,
                    satisfied=True,
                    reason="Stub evidence accepted",
                )
                for index, _ in enumerate(step.success_criteria)
            ],
        )

    def verify_task(self, *, task_spec, step_results, task_results):
        return VerificationResult(completed=True, reason="Stub task accepted")


@pytest.mark.asyncio
async def test_graph_initializes_task() -> None:
    initial_state: TaskState = {
        "task_id": "task-001",
        "user_input": "Find three gyms and generate a CSV",
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

    result = await build_graph(
        analyzer=StubAnalyzer(),
        planner=StubPlanner(),
        executor=StubExecutor(),
        verifier=StubVerifier(),
    ).ainvoke(initial_state)

    assert result["task_id"] == initial_state["task_id"]
    assert result["user_input"] == initial_state["user_input"]
    assert result["status"] is TaskStatus.COMPLETED
    assert result["task_spec"] is not None
    assert len(result["plan"]) == 1
    assert result["current_step_id"] is None
    assert result["step_outcome"] is None
    assert result["step_results"][0].output == {"prepared": True}
