"""Initial Planner 与 Phase 3 状态图的离线测试。"""

from unittest.mock import Mock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel

from taskpilot.graph import build_graph
from taskpilot.models import PlanStep, TaskPlan, TaskSpec, TaskStatus
from taskpilot.planner import PLANNER_SYSTEM_PROMPT, TaskPlanner, validate_plan
from taskpilot.state import TaskState


USER_INPUT = "找3家香港岛月费低于500港币的健身房，并生成CSV。"


def make_task_spec() -> TaskSpec:
    """构造 Fake Analyzer 的固定输出。"""

    return TaskSpec(
        goal="查找满足条件的健身房并生成CSV",
        constraints={
            "location": "香港岛",
            "max_monthly_price_hkd": 500,
            "count": 3,
        },
        expected_output="csv",
        completion_criteria=[
            "至少返回3家符合要求的健身房",
            "候选位于香港岛且月费不超过500港币",
            "最终输出为CSV",
        ],
    )


def make_task_plan() -> TaskPlan:
    """构造工具无关且依赖合法的固定计划。"""

    return TaskPlan(
        steps=[
            PlanStep(
                id=1,
                description="识别符合地点和价格约束的候选健身房",
                success_criteria=["获得不少于3个可进一步核实的候选"],
            ),
            PlanStep(
                id=2,
                description="获取候选健身房的价格、地址和营业时间",
                depends_on=[1],
                success_criteria=[
                    "获得候选的价格信息",
                    "获得候选的地址信息",
                    "获得候选的营业时间信息",
                ],
            ),
            PlanStep(
                id=3,
                description="根据任务约束筛选并确认最终候选",
                depends_on=[2],
                success_criteria=[
                    "最终候选数量不少于3家",
                    "最终候选满足地点和价格约束",
                ],
            ),
            PlanStep(
                id=4,
                description="把最终候选整理为CSV结果",
                depends_on=[3],
                success_criteria=["CSV包含最终候选及其关键信息"],
            ),
        ]
    )


def make_initial_state() -> TaskState:
    """构造 Phase 3 图调用所需的初始状态。"""

    return {
        "task_id": "task-phase-3",
        "user_input": USER_INPUT,
        "task_spec": None,
        "plan": [],
        "current_step_id": None,
        "tool_calls": [],
        "task_results": {},
        "verification": None,
        "status": TaskStatus.CREATED,
        "error": None,
    }


class FakeAnalyzer:
    """记录调用次数并返回固定 TaskSpec。"""

    def __init__(self) -> None:
        self.call_count = 0

    def analyze(self, user_input: str) -> TaskSpec:
        assert user_input == USER_INPUT
        self.call_count += 1
        return make_task_spec()


class FakePlanner:
    """记录调用次数并返回固定 TaskPlan。"""

    def __init__(self) -> None:
        self.call_count = 0

    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        assert task_spec == make_task_spec()
        self.call_count += 1
        return make_task_plan()


def test_planner_writes_plan_to_graph_state() -> None:
    result = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
    ).invoke(make_initial_state())

    assert result["task_spec"] == make_task_spec()
    assert result["plan"] == make_task_plan().steps
    assert result["current_step_id"] == 1
    assert result["status"] is TaskStatus.RUNNING
    assert result["error"] is None


def test_graph_calls_analyzer_and_planner_once() -> None:
    analyzer = FakeAnalyzer()
    planner = FakePlanner()

    build_graph(analyzer=analyzer, planner=planner).invoke(make_initial_state())

    assert analyzer.call_count == 1
    assert planner.call_count == 1


def test_task_planner_uses_structured_output() -> None:
    structured_model = Mock()
    structured_model.invoke.return_value = make_task_plan()
    model = Mock(spec=BaseChatModel)
    model.with_structured_output.return_value = structured_model

    result = TaskPlanner(model=model).plan(make_task_spec())

    assert result == make_task_plan()
    model.with_structured_output.assert_called_once_with(TaskPlan)
    structured_model.invoke.assert_called_once()


def test_valid_plan_is_tool_agnostic_and_dependencies_are_legal() -> None:
    task_plan = make_task_plan()

    validate_plan(task_plan)

    descriptions = " ".join(step.description.lower() for step in task_plan.steps)
    assert "browser_open" not in descriptions
    assert "search(" not in descriptions
    assert "css selector" not in descriptions
    assert [step.depends_on for step in task_plan.steps] == [[], [1], [2], [3]]


def test_plan_step_success_criteria_are_not_shared() -> None:
    first = PlanStep(id=1, description="First")
    second = PlanStep(id=2, description="Second")

    first.success_criteria.append("First is complete")

    assert first.success_criteria == ["First is complete"]
    assert second.success_criteria == []


def test_validate_plan_rejects_duplicate_step_ids() -> None:
    task_plan = TaskPlan(
        steps=[
            PlanStep(id=1, description="First", success_criteria=["First done"]),
            PlanStep(id=1, description="Second", success_criteria=["Second done"]),
        ]
    )

    with pytest.raises(ValueError, match="id 必须唯一"):
        validate_plan(task_plan)


def test_validate_plan_rejects_missing_dependency() -> None:
    task_plan = TaskPlan(
        steps=[
            PlanStep(id=1, description="First", success_criteria=["First done"]),
            PlanStep(
                id=2,
                description="Second",
                depends_on=[99],
                success_criteria=["Second done"],
            ),
        ]
    )

    with pytest.raises(ValueError, match="不存在的依赖 99"):
        validate_plan(task_plan)


def test_validate_plan_rejects_self_dependency() -> None:
    task_plan = TaskPlan(
        steps=[
            PlanStep(id=1, description="First", success_criteria=["First done"]),
            PlanStep(
                id=2,
                description="Second",
                depends_on=[2],
                success_criteria=["Second done"],
            ),
        ]
    )

    with pytest.raises(ValueError, match="不能依赖自身"):
        validate_plan(task_plan)


def test_validate_plan_rejects_forward_dependency() -> None:
    task_plan = TaskPlan(
        steps=[
            PlanStep(
                id=1,
                description="First",
                depends_on=[2],
                success_criteria=["First done"],
            ),
            PlanStep(id=2, description="Second", success_criteria=["Second done"]),
        ]
    )

    with pytest.raises(ValueError, match="必须出现在当前步骤之前"):
        validate_plan(task_plan)


def test_validate_plan_rejects_empty_steps() -> None:
    with pytest.raises(ValueError, match="steps 不能为空"):
        validate_plan(TaskPlan(steps=[]))


def test_validate_plan_requires_step_success_criteria() -> None:
    task_plan = TaskPlan(steps=[PlanStep(id=1, description="First")])

    with pytest.raises(ValueError, match="缺少 success_criteria"):
        validate_plan(task_plan)


def test_planner_failure_is_recorded_without_plan() -> None:
    class FailingPlanner:
        def plan(self, task_spec: TaskSpec) -> TaskPlan:
            raise RuntimeError("planner unavailable")

    result = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FailingPlanner(),
    ).invoke(make_initial_state())

    assert result["status"] is TaskStatus.FAILED
    assert result["error"] == (
        "Task Planner 调用失败: RuntimeError: planner unavailable"
    )
    assert result["plan"] == []
    assert result["current_step_id"] is None


def test_planner_failure_redacts_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "phase-3-secret-key")

    class FailingPlanner:
        def plan(self, task_spec: TaskSpec) -> TaskPlan:
            raise RuntimeError("request failed for phase-3-secret-key")

    result = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FailingPlanner(),
    ).invoke(make_initial_state())

    assert "phase-3-secret-key" not in result["error"]
    assert "[REDACTED]" in result["error"]


def test_planner_prompt_preserves_planning_boundary() -> None:
    assert "只负责" in PLANNER_SYSTEM_PROMPT
    assert "不执行任务" in PLANNER_SYSTEM_PROMPT
    assert "不得包含" in PLANNER_SYSTEM_PROMPT
    assert "具体工具调用" in PLANNER_SYSTEM_PROMPT
    assert "success_criteria" in PLANNER_SYSTEM_PROMPT
    assert "不得添加 TaskSpec 中不存在的硬约束" in PLANNER_SYSTEM_PROMPT
    assert "不得虚构" in PLANNER_SYSTEM_PROMPT
