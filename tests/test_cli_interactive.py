"""TaskPilot 常驻交互 Shell 测试。"""

import argparse
from unittest.mock import AsyncMock

import pytest

import taskpilot.cli as cli
from taskpilot.trace import TraceEventType, make_trace_event


def shell_args(**overrides: object) -> argparse.Namespace:
    values = {
        "state_dir": cli.Path(".taskpilot"),
        "mcp_config": None,
        "filesystem_root": None,
        "browser": True,
        "headed": True,
        "recursion_limit": 500,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_interactive_task_argv_preserves_shell_capabilities() -> None:
    args = shell_args(
        mcp_config=cli.Path("mcp.json"),
        filesystem_root=cli.Path("artifacts"),
    )

    argv = cli.interactive_task_argv(args, "搜索华为招聘")

    assert argv == [
        "搜索华为招聘",
        "--state-dir",
        ".taskpilot",
        "--mcp-config",
        "mcp.json",
        "--filesystem-root",
        "artifacts",
        "--browser",
        "--headed",
        "--recursion-limit",
        "500",
    ]


def test_startup_banner_displays_model_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("LLM_API_KEY", "super-secret-key")

    banner = cli.startup_banner(shell_args())

    assert "TaskPilot" not in banner  # 名称由 ASCII 字形呈现
    assert "deepseek-v4-flash" in banner
    assert "Browser : ON · VISIBLE" in banner
    assert "super-secret-key" not in banner


def test_progress_distinguishes_executor_claim_from_verification() -> None:
    executor_message = cli.render_progress_event(
        make_trace_event(
            task_id="t",
            event_type=TraceEventType.LLM_ACTION_DECIDED,
            step_id=2,
            latency_ms=12.0,
            payload={"action_type": "finish_step"},
        )
    )
    verifier_message = cli.render_progress_event(
        make_trace_event(
            task_id="t",
            event_type=TraceEventType.STEP_VERIFIED,
            step_id=2,
            latency_ms=4.0,
        )
    )

    assert executor_message is not None
    assert "awaiting step verification" in executor_message
    assert "passed" not in executor_message
    assert verifier_message is not None
    assert "verification passed" in verifier_message


def test_progress_renders_step_position() -> None:
    message = cli.render_progress_event(
        make_trace_event(
            task_id="t",
            event_type=TraceEventType.STEP_STARTED,
            step_id=7,
            payload={"step_position": 2, "step_total": 4},
        )
    )

    assert message is not None
    assert "step=7" in message
    assert "progress=2/4" in message


def test_progress_error_is_redacted_and_compact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "progress-secret")
    message = cli.render_progress_event(
        make_trace_event(
            task_id="t",
            event_type=TraceEventType.TASK_FAILED,
            payload={"error": "progress-secret " + "x" * 1_000},
        )
    )

    assert message is not None
    assert "progress-secret" not in message
    assert "[REDACTED]" in message
    assert len(message) < 300


def test_progress_reports_duplicate_observation_correction() -> None:
    message = cli.render_progress_event(
        make_trace_event(
            task_id="t",
            event_type=TraceEventType.EXECUTOR_CORRECTED,
            step_id=1,
            payload={"reason": "same page and content"},
        )
    )

    assert message is not None
    assert "duplicate observation skipped" in message
    assert "same page and content" in message


def test_interactive_task_argv_preserves_verbose_mode() -> None:
    argv = cli.interactive_task_argv(shell_args(verbose=True), "task")

    assert "--verbose" in argv


@pytest.mark.asyncio
async def test_interactive_shell_runs_multiple_tasks_until_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["第一个任务", "", "/help", "第二个任务", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    run_once = AsyncMock(return_value=0)
    monkeypatch.setattr(cli, "async_main", run_once)

    exit_code = await cli.interactive_shell(shell_args())

    assert exit_code == 0
    assert run_once.await_count == 2
    assert run_once.await_args_list[0].args[0][0] == "第一个任务"
    assert run_once.await_args_list[1].args[0][0] == "第二个任务"


@pytest.mark.asyncio
async def test_interactive_shell_contains_task_exception_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["失败任务", "后续任务", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    run_once = AsyncMock(side_effect=[RuntimeError("controlled failure"), 0])
    monkeypatch.setattr(cli, "async_main", run_once)

    exit_code = await cli.interactive_shell(shell_args())

    assert exit_code == 0
    assert run_once.await_count == 2


@pytest.mark.asyncio
async def test_interactive_shell_handles_ctrl_c_at_prompt_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用户在等待输入时按 Ctrl+C，应正常退出且不运行新任务。"""
    def interrupted_input(prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupted_input)
    run_once = AsyncMock()
    monkeypatch.setattr(cli, "async_main", run_once)

    assert await cli.interactive_shell(shell_args()) == 130
    run_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_ctrl_c_while_task_running_exits_shell_not_next_task(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """执行任务时按 Ctrl+C，直接退出整个 Shell，不继续读取下一条任务。"""
    user_inputs: list[str] = ["首个任务", "不该被执行的任务"]
    def next_input(prompt: str = "") -> str:
        return user_inputs.pop(0)

    monkeypatch.setattr("builtins.input", next_input)
    run_once = AsyncMock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr(cli, "async_main", run_once)

    assert await cli.interactive_shell(shell_args()) == 130
    assert run_once.await_count == 1
    assert user_inputs == ["不该被执行的任务"]
    assert "已退出 TaskPilot" in capsys.readouterr().out


def test_main_handles_ctrl_c_during_asyncio_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """模拟运行中的任务被 SIGINT 取消：顶层必须吞掉异常堆栈并退出。"""
    def interrupted_run(coro: object) -> None:
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.asyncio, "run", interrupted_run)

    assert cli.main([]) == 130
    assert "已退出 TaskPilot" in capsys.readouterr().out



def test_console_input_restores_original_sigint_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """控制台输入完成或收到 KeyboardInterrupt 时均需恢复 asyncio 的 SIGINT handler。"""
    import signal

    previous = signal.getsignal(signal.SIGINT)
    monkeypatch.setattr("builtins.input", lambda prompt="": "hello")
    assert cli._console_input("taskpilot> ") == "hello"
    assert signal.getsignal(signal.SIGINT) is previous

    def interrupted(prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupted)
    with pytest.raises(KeyboardInterrupt):
        cli._console_input("taskpilot> ")
    assert signal.getsignal(signal.SIGINT) is previous
