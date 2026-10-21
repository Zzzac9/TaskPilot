"""Terminal task-plan visibility and monotonic per-task runtime metrics."""

from unittest.mock import AsyncMock

import pytest

import taskpilot.cli as cli
from taskpilot.graph import plan_task_traced
from taskpilot.models import PlanStep, TaskPlan, TaskSpec
from taskpilot.service import create_initial_state
from taskpilot.trace import InMemoryTraceRecorder, TraceEventType, make_trace_event


def steps_payload():
    return [{
        "id": 1,
        "description": "读取两个候选网页",
        "depends_on": [],
        "success_criteria": ["获得可信价格", "找到候选名称"],
        "requirement_ids": ["R1"],
    }, {
        "id": 2,
        "description": "结合报价和库存筛选结果",
        "depends_on": [1],
        "success_criteria": ["满足价格条件"],
        "requirement_ids": ["R2", "R3"],
    }]


def test_initial_plan_is_printed_in_full_before_execution() -> None:
    message = cli.render_progress_event(make_trace_event(
        task_id="t-1",
        event_type=TraceEventType.PLAN_CREATED,
        latency_ms=10,
        payload={"step_ids": [1, 2], "steps": steps_payload()},
        status="success",
    ))
    assert message is not None
    assert "plan ready steps=2" in message
    assert "1. 读取两个候选网页" in message
    assert "2. 结合报价和库存筛选结果" in message
    assert "依赖步骤：1" in message
    assert "获得可信价格；找到候选名称" in message
    assert "[要求：R2, R3]" in message


def test_replan_shows_only_replacement_steps_on_success() -> None:
    event = make_trace_event(
        task_id="t-2",
        event_type=TraceEventType.PLAN_REPLANNED,
        status="success",
        plan_revision=1,
        payload={"steps": [{
            "id": 7, "description": "从已有只读证据继续",
            "depends_on": [1], "success_criteria": ["结果准确"],
            "requirement_ids": ["R1"],
        }]},
    )
    message = cli.render_progress_event(event)
    assert message is not None
    assert "plan revised status=success" in message
    assert "7. 从已有只读证据继续" in message
    failure = cli.render_progress_event(event.model_copy(update={"status": "failed"}))
    assert failure is not None
    assert "7. 从已有只读证据继续" not in failure


@pytest.mark.asyncio
async def test_plan_node_trace_carries_real_step_descriptions() -> None:
    class Planner:
        def plan(self, task_spec: TaskSpec) -> TaskPlan:
            return TaskPlan(steps=[
                PlanStep(id=1, description="检查来源 A", success_criteria=["真实来源"]),
                PlanStep(id=2, description="检查来源 B", depends_on=[1],
                         success_criteria=["两个来源匹配"]),
            ])

    state = create_initial_state("找两个来源")
    state["task_spec"] = TaskSpec(goal="找两个来源")
    recorder = InMemoryTraceRecorder()
    update = await plan_task_traced(state, planner=Planner(), trace_recorder=recorder)
    assert len(update["plan"]) == 2
    event = recorder.events[0]
    assert event.event_type is TraceEventType.PLAN_CREATED
    assert event.payload["step_ids"] == [1, 2]
    assert event.payload["steps"][1]["depends_on"] == [1]
    assert "检查来源 B" in (cli.render_progress_event(event) or "")


@pytest.mark.asyncio
async def test_total_elapsed_after_a_normal_task_and_even_if_task_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_run = AsyncMock(return_value=0)
    monkeypatch.setattr(cli, "_async_main_impl", mock_run)
    clock = iter([1.25, 4.50, 10.00, 14.25])
    monkeypatch.setattr(cli, "perf_counter", lambda: next(clock))

    assert await cli.async_main(["提取两个候选"]) == 0
    assert "任务总耗时：3.25 秒" in capsys.readouterr().out

    mock_run.side_effect = RuntimeError("controlled failure")
    with pytest.raises(RuntimeError, match="controlled failure"):
        await cli.async_main(["第二个任务"])
    assert "任务总耗时：4.25 秒" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_resume_timing_and_shell_do_not_time_the_whole_session(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_run = AsyncMock(return_value=0)
    monkeypatch.setattr(cli, "_async_main_impl", mock_run)
    clock = iter([1.0, 3.0])
    monkeypatch.setattr(cli, "perf_counter", lambda: next(clock))

    assert await cli.async_main(["--resume", "task-id"]) == 0
    assert "本次恢复耗时：2.00 秒" in capsys.readouterr().out
    assert await cli.async_main(["--interactive"]) == 0
    assert "耗时" not in capsys.readouterr().out
    assert mock_run.await_count == 2


@pytest.mark.asyncio
async def test_cli_result_precedes_duration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """真实 async_main 输出路径中先展示任务产物，再展示总耗时。"""
    from types import SimpleNamespace

    terminal_state = {
        "task_id": "task-smoke",
        "status": "completed",
        "final_answer": "已验证的结果",
        "error": None,
        "step_outcome": None,
    }
    fake_run = AsyncMock(return_value=SimpleNamespace(
        state=terminal_state,
        interrupt=None,
    ))
    fake_service = SimpleNamespace(run_new_task=fake_run)
    monkeypatch.setattr(cli, "TaskPilotService", lambda *args, **kwargs: fake_service)
    clock = iter([20.0, 22.0])
    monkeypatch.setattr(cli, "perf_counter", lambda: next(clock))

    assert await cli.async_main(["读取简单结果"]) == 0
    output = capsys.readouterr().out
    assert "task_id=task-smoke" in output
    assert "status=completed" in output
    assert "final_output:" in output
    assert output.index("已验证的结果") < output.index("任务总耗时：2.00 秒")
