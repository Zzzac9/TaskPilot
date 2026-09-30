"""Task Analyzer 与 Phase 2 图的离线测试。"""

import pytest

from taskpilot.analyzer import ANALYZER_SYSTEM_PROMPT
from taskpilot.executor import ExecutorRunResult
from taskpilot.graph import analyze_task, build_graph
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


USER_INPUT = "找3家香港岛月费低于500港币的健身房，并生成CSV。"


def make_task_spec() -> TaskSpec:
    """构造可预测的离线分析结果。"""

    return TaskSpec(
        goal="查找满足条件的健身房并生成结果文件",
        constraints={
            "location": "香港岛",
            "max_monthly_price_hkd": 500,
            "count": 3,
        },
        expected_output="csv",
        completion_criteria=[
            "至少找到3家健身房",
            "候选健身房位于香港岛",
            "月费不超过500港币",
            "最终输出为CSV",
        ],
    )


def make_initial_state() -> TaskState:
    """构造图调用所需的完整初始状态。"""

    return {
        "task_id": "task-phase-2",
        "user_input": USER_INPUT,
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


class FakeAnalyzer:
    """记录调用次数并返回固定 TaskSpec。"""

    def __init__(self, task_spec: TaskSpec) -> None:
        self.task_spec = task_spec
        self.call_count = 0

    def analyze(self, user_input: str) -> TaskSpec:
        assert user_input == USER_INPUT
        self.call_count += 1
        return self.task_spec


class FakePlanner:
    """为 Analyzer 图测试提供最小有效计划。"""

    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="完成结构化任务目标",
                    success_criteria=["任务目标所需结果已准备"],
                )
            ]
        )


class FakeExecutor:
    """为 Analyzer 图测试提供最小步骤执行声明。"""

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
                summary="Fake execution",
                evidence=["Fake evidence"],
                output={"result": "fake"},
                action_count=1,
            )
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
                    reason="Accepted offline",
                )
                for index, _ in enumerate(step.success_criteria)
            ],
        )

    def verify_task(self, *, task_spec, step_results, task_results):
        return VerificationResult(completed=True, reason="Accepted offline")


def test_analyze_task_writes_structured_task_spec() -> None:
    expected = make_task_spec()
    update = analyze_task(make_initial_state(), analyzer=FakeAnalyzer(expected))

    task_spec = update["task_spec"]
    assert task_spec == expected
    assert task_spec.goal == "查找满足条件的健身房并生成结果文件"
    assert task_spec.constraints["max_monthly_price_hkd"] == 500
    assert task_spec.expected_output == "csv"
    assert task_spec.completion_criteria


@pytest.mark.asyncio
async def test_graph_runs_initialize_then_analyze() -> None:
    initial_state = make_initial_state()
    result = await build_graph(
        analyzer=FakeAnalyzer(make_task_spec()),
        planner=FakePlanner(),
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
    ).ainvoke(initial_state)

    assert result["task_id"] == initial_state["task_id"]
    assert result["user_input"] == initial_state["user_input"]
    assert result["status"] is TaskStatus.COMPLETED
    assert result["task_spec"] is not None


@pytest.mark.asyncio
async def test_graph_calls_analyzer_once() -> None:
    analyzer = FakeAnalyzer(make_task_spec())

    await build_graph(
        analyzer=analyzer,
        planner=FakePlanner(),
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
    ).ainvoke(
        make_initial_state()
    )

    assert analyzer.call_count == 1


def test_analyzer_prompt_preserves_scope_and_user_constraints() -> None:
    assert "不要规划或执行任务" in ANALYZER_SYSTEM_PROMPT
    assert "不要添加用户未表达的硬约束" in ANALYZER_SYSTEM_PROMPT
    assert "expected_output 应为 null" in ANALYZER_SYSTEM_PROMPT
    assert "可验证的成功条件" in ANALYZER_SYSTEM_PROMPT
    assert "不能写成搜索、点击、提取等执行步骤" in ANALYZER_SYSTEM_PROMPT


def test_analyzer_failure_is_recorded_in_state() -> None:
    class FailingAnalyzer:
        def analyze(self, user_input: str) -> TaskSpec:
            raise RuntimeError("model unavailable")

    update = analyze_task(make_initial_state(), analyzer=FailingAnalyzer())

    assert update["status"] is TaskStatus.FAILED
    assert update["error"] == (
        "Task Analyzer 调用失败: RuntimeError: model unavailable"
    )
