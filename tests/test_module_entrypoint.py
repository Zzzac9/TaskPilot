"""`python -m taskpilot` 默认参数测试。"""

from unittest.mock import patch

from taskpilot.__main__ import DEFAULT_INTERACTIVE_ARGS, entrypoint, resolve_argv


def test_zero_arguments_enable_interactive_headed_browser() -> None:
    assert resolve_argv([]) == list(DEFAULT_INTERACTIVE_ARGS)
    assert resolve_argv([]) == ["--interactive", "--browser", "--headed"]


def test_explicit_arguments_are_forwarded_without_changes() -> None:
    args = ["--resume", "task-123", "--state-dir", "state"]
    assert resolve_argv(args) == args


def test_entrypoint_passes_default_arguments_to_cli() -> None:
    with patch("taskpilot.__main__.main", return_value=0) as cli_main:
        assert entrypoint([]) == 0

    cli_main.assert_called_once_with(
        ["--interactive", "--browser", "--headed"]
    )
