"""TaskPilot 的 async Host/CLI 入口。"""

import argparse
import asyncio
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

from taskpilot.browser import BrowserConfig
from taskpilot.config import Settings, redact_sensitive_values
from taskpilot.filesystem_mcp import create_filesystem_mcp_config
from taskpilot.mcp.config import load_mcp_server_configs
from taskpilot.models import TaskStatus
from taskpilot.persistence import PersistenceConfig
from taskpilot.service import (
    TaskConflictError,
    TaskNotFoundError,
    TaskPilotService,
    create_initial_state as create_initial_state,
)
from taskpilot.trace import TraceEvent, TraceEventType
from taskpilot.vision import VisionConfig


_PROGRESS_NODE_LABELS = {
    "analyze_task": "分析任务",
    "plan_task": "生成计划",
    "decide_action": "决定下一动作",
    "execute_tool_action": "执行工具",
    "verify_step": "验证当前步骤",
    "verify_task": "验证整个任务",
    "replan_task": "重新规划",
    "prepare_approval": "准备人工审批",
    "human_approval": "处理人工审批",
    "recovery_review": "检查崩溃恢复",
    "human_assistance": "等待并复核人工协助",
}


def _progress_latency(event: TraceEvent) -> str:
    return f" ({event.latency_ms:.0f} ms)" if event.latency_ms is not None else ""


def _progress_detail(value: Any, *, max_chars: int = 240) -> str:
    detail = (
        redact_sensitive_values(str(value or "")).replace("\r", " ").replace("\n", " ")
    )
    return detail if len(detail) <= max_chars else detail[:max_chars] + "…"


def render_progress_event(event: TraceEvent) -> str | None:
    """把已脱敏 Trace 转成紧凑进度；执行完成声明不冒充验证通过。"""

    prefix = "[progress]"
    step = f" step={event.step_id}" if event.step_id is not None else ""
    latency = _progress_latency(event)
    payload = event.payload
    if event.event_type is TraceEventType.NODE_STARTED:
        node_name = str(payload.get("node_name") or "")
        label = _PROGRESS_NODE_LABELS.get(node_name)
        return f"{prefix} phase={label}{step}" if label else None
    if event.event_type is TraceEventType.TASK_STARTED:
        return f"{prefix} task started"
    if event.event_type is TraceEventType.TASK_ANALYZED:
        return f"{prefix} task analysis {event.status}{latency}"
    if event.event_type is TraceEventType.PLAN_CREATED:
        return (
            f"{prefix} plan ready steps={len(payload.get('step_ids') or [])}{latency}"
        )
    if event.event_type is TraceEventType.PLAN_REPLANNED:
        return f"{prefix} plan revised status={event.status}{latency}"
    if event.event_type is TraceEventType.STEP_STARTED:
        position = payload.get("step_position")
        total = payload.get("step_total")
        progress = f" progress={position}/{total}" if position and total else ""
        return f"{prefix} step started{step}{progress}"
    if event.event_type is TraceEventType.LLM_ACTION_DECIDED:
        action_type = payload.get("action_type")
        if action_type == "finish_step":
            return (
                f"{prefix} executor result prepared{step}; "
                "awaiting step verification"
                f"{latency}"
            )
        tool_name = payload.get("tool_name")
        return f"{prefix} executor selected tool={tool_name}{step}{latency}"
    if event.event_type is TraceEventType.EXECUTOR_CORRECTED:
        reason = _progress_detail(payload.get("reason"))
        return f"{prefix} duplicate observation skipped{step} reason={reason}"
    if event.event_type is TraceEventType.TOOL_STARTED:
        return f"{prefix} tool started name={payload.get('tool_name')}{step}"
    if event.event_type is TraceEventType.TOOL_SUCCEEDED:
        return f"{prefix} tool succeeded name={payload.get('tool_name')}{step}{latency}"
    if event.event_type is TraceEventType.TOOL_FAILED:
        error = _progress_detail(payload.get("error"))
        return (
            f"{prefix} tool failed name={payload.get('tool_name')}{step}{latency}"
            f" error={error}"
        )
    if event.event_type is TraceEventType.TOOL_MATERIALIZED_FROM_JOURNAL:
        return f"{prefix} tool result restored name={payload.get('tool_name')}{step}"
    if event.event_type is TraceEventType.STEP_VERIFIED:
        return f"{prefix} step verification passed{step}{latency}"
    if event.event_type is TraceEventType.STEP_REJECTED:
        feedback = _progress_detail(payload.get("feedback"))
        return f"{prefix} step verification rejected{step}{latency} feedback={feedback}"
    if event.event_type is TraceEventType.TASK_VERIFIED:
        return f"{prefix} task verification passed; preparing final output{latency}"
    if event.event_type is TraceEventType.TASK_VERIFICATION_REJECTED:
        reason = _progress_detail(payload.get("reason"))
        return f"{prefix} task verification rejected{latency} reason={reason}"
    if event.event_type is TraceEventType.FINAL_ANSWER_GENERATED:
        return f"{prefix} final output generation {event.status}{latency}"
    if event.event_type is TraceEventType.FAILURE_SUMMARY_GENERATED:
        return f"{prefix} failure summary prepared{step}"
    if event.event_type is TraceEventType.APPROVAL_REQUESTED:
        return f"{prefix} waiting for approval tool={payload.get('tool_name')}{step}"
    if event.event_type is TraceEventType.RECOVERY_REQUIRED:
        return (
            f"{prefix} recovery review required tool={payload.get('tool_name')}{step}"
        )
    if event.event_type is TraceEventType.HUMAN_ASSISTANCE_REQUESTED:
        return f"{prefix} human assistance requested{step}"
    if event.event_type is TraceEventType.HUMAN_ASSISTANCE_RESOLVED:
        return f"{prefix} human assistance {event.status}{step}"
    if event.event_type is TraceEventType.TASK_COMPLETED:
        return f"{prefix} task completed after verification"
    if event.event_type is TraceEventType.TASK_FAILED:
        error = _progress_detail(payload.get("error"))
        return f"{prefix} task failed error={error}"
    return None


def print_progress_event(event: TraceEvent) -> None:
    message = render_progress_event(event)
    if message is not None:
        print(message, flush=True)


def render_final_output(result: Mapping[str, Any]) -> str | None:
    """把任务级结果或最后一步正式产物渲染为面向用户的完整输出。"""

    final_answer = result.get("final_answer")
    if isinstance(final_answer, str) and final_answer.strip():
        return final_answer.strip()
    task_results = result.get("task_results")
    if task_results:
        payload: Any = task_results
        summary = None
    else:
        step_results = result.get("step_results") or []
        if not step_results:
            return None
        final_step = step_results[-1]
        summary = final_step.summary
        payload = final_step.output

    parts: list[str] = []
    if summary:
        parts.append(str(summary))
    parts.append(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return "\n".join(parts)


def configure_console_encoding() -> None:
    """Windows 控制台统一输出 UTF-8，网页字符不应截断最终结果。"""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def interactive_task_argv(args: argparse.Namespace, task: str) -> list[str]:
    """把交互 Shell 的默认能力转换为一次普通 CLI task 参数。"""

    argv = [task, "--state-dir", str(args.state_dir)]
    if args.mcp_config is not None:
        argv.extend(["--mcp-config", str(args.mcp_config)])
    if getattr(args, "filesystem_root", None) is not None:
        argv.extend(["--filesystem-root", str(args.filesystem_root)])
    if args.browser:
        argv.append("--browser")
    if args.headed:
        argv.append("--headed")
    if getattr(args, "verbose", False):
        argv.append("--verbose")
    argv.extend(["--recursion-limit", str(args.recursion_limit)])
    return argv


def startup_banner(args: argparse.Namespace) -> str:
    """生成不包含凭证的交互式启动 Banner。"""

    settings = Settings.from_env()
    model = (settings.llm_model or "未配置").replace("\r", " ").replace("\n", " ")
    browser = "ON" if args.browser else "OFF"
    window = "VISIBLE" if args.headed else "HEADLESS"
    return f"""
                         __|__
------------------o--o--(_)--o--o------------------

 _____         _    ____  _ _       _
|_   _|_ _ ___| | _|  _ \\(_) | ___ | |_
  | |/ _` / __| |/ / |_) | | |/ _ \\| __|
  | | (_| \\__ \\   <|  __/| | | (_) | |_
  |_|\\__,_|___/_|\\_\\_|   |_|_|\\___/ \\__|

  Model   : {model}
  Browser : {browser} · {window}
----------------------------------------------------""".strip("\n")


def _console_input(prompt: str) -> str:
    """同步读取 CLI 输入，并在等待输入时允许一次 Ctrl+C 立即退出。

    asyncio.run 默认的 SIGINT handler 只标记取消 coroutine；如果此时
    input() 正阻塞主线程，任务取消不会立即执行。仅在输入期间恢复
    Python 默认 SIGINT 行为，结束后还原原 handler，不影响执行期清理。
    """
    is_main_thread = threading.current_thread() is threading.main_thread()
    previous_handler = signal.getsignal(signal.SIGINT) if is_main_thread else None
    if is_main_thread:
        signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        return input(prompt)
    finally:
        if is_main_thread:
            signal.signal(signal.SIGINT, previous_handler)


async def interactive_shell(args: argparse.Namespace) -> int:
    """常驻读取多个独立任务；每条任务沿用同一组 Host 能力参数。"""

    print(startup_banner(args))
    print("输入任务并回车；输入 /help 查看帮助，/exit 或 Ctrl+C 退出。")
    while True:
        try:
            # Shell 在此刻没有并行任务：直接读取标准输入，使 Ctrl+C 可在本层捕获。
            # asyncio.to_thread(input) 在 asyncio.run 收到 Ctrl+C 时容易产生 CancelledError traceback。
            raw = _console_input("taskpilot> ")
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            # Ctrl+C 始终是退出整个 CLI，不是回到下一轮输入。
            print("\n已退出 TaskPilot。")
            return 130
        task = raw.strip()
        if not task:
            continue
        if task.lower() in {"/exit", "/quit", "exit", "quit"}:
            return 0
        if task.lower() == "/help":
            print("直接输入自然语言任务；每次输入都会创建独立 task。")
            print("/exit 或 /quit：退出 TaskPilot。")
            continue
        try:
            await async_main(interactive_task_argv(args, task))
        except KeyboardInterrupt:
            # 当前任务同步收到 Ctrl+C 时也必须退出 Shell，不能继续等待任务输入。
            print("\n已退出 TaskPilot。")
            return 130
        except Exception as exc:
            detail = redact_sensitive_values(str(exc))
            print(f"task_error={type(exc).__name__}: {detail}")
            print("当前任务失败，交互 Shell 继续运行。")
        print()


async def async_main(argv: Sequence[str] | None = None) -> int:
    """在可选 MCP/Browser 生命周期内异步运行一次 TaskPilot Graph。"""

    parser = argparse.ArgumentParser(description="Run the TaskPilot graph.")
    parser.add_argument("task", nargs="?", help="Task description for a new task")
    parser.add_argument(
        "--resume",
        metavar="TASK_ID",
        help="Resume an existing durable LangGraph thread",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".taskpilot"),
        help=(
            "Directory containing checkpoints.sqlite3, actions.sqlite3, "
            "and traces.sqlite3"
        ),
    )
    parser.add_argument(
        "--mcp-config",
        type=Path,
        help="JSON file containing MCP server configurations",
    )
    parser.add_argument(
        "--filesystem-root",
        type=Path,
        help=(
            "Enable the bundled Filesystem MCP and restrict all file access "
            "to this directory"
        ),
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Enable the Playwright Chromium Browser Tools",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the Chromium window (requires --browser)",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Start a persistent prompt for multiple independent tasks",
    )
    parser.add_argument(
        "--recursion-limit",
        type=int,
        default=500,
        help="Maximum LangGraph transitions for one invocation (default: 500)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print the full plan, execution state, and trace summary",
    )
    args = parser.parse_args(argv)

    if args.headed and not args.browser:
        parser.error("--headed requires --browser")
    if args.interactive:
        if args.task is not None or args.resume is not None:
            parser.error("--interactive cannot be combined with TASK or --resume")
        return await interactive_shell(args)
    if (args.task is None) == (args.resume is None):
        parser.error("provide exactly one of TASK or --resume TASK_ID")

    # CLI 只负责参数与展示，实际执行、恢复及安全校验统一交给 TaskPilotService。
    persistence_config = PersistenceConfig.from_state_dir(args.state_dir)
    mcp_configs = (
        load_mcp_server_configs(args.mcp_config) if args.mcp_config is not None else []
    )
    if args.filesystem_root is not None:
        mcp_configs.append(create_filesystem_mcp_config(args.filesystem_root))
    service = TaskPilotService(
        persistence_config,
        mcp_configs=mcp_configs,
        browser_allowed=args.browser,
        browser_config=BrowserConfig(headless=not args.headed),
        vision_config=VisionConfig.from_env(),
        graph_recursion_limit=args.recursion_limit,
    )
    try:
        if args.task is not None:
            run = await service.run_new_task(
                args.task,
                enable_browser=args.browser,
                progress_handler=print_progress_event,
            )
        else:
            assert args.resume is not None
            run = await service.get_task(args.resume)
            restored_status = run.state.get("status")
            if restored_status in {TaskStatus.COMPLETED, TaskStatus.COMPLETED.value}:
                print("task already completed")
            elif (
                restored_status in {TaskStatus.FAILED, TaskStatus.FAILED.value}
                and not run.next_nodes
            ):
                print("task already failed; automatic restart is disabled")
            elif run.next_nodes and run.interrupt is None:
                run = await service.continue_task(
                    args.resume,
                    progress_handler=print_progress_event,
                )
    except TaskNotFoundError:
        print(f"error=no durable checkpoint found for task_id={args.resume}")
        return 1
    except TaskConflictError as exc:
        print(f"error={exc}")
        return 1
    except Exception as exc:
        detail = redact_sensitive_values(str(exc))
        print(f"error=Task runtime failed: {type(exc).__name__}: {detail}")
        return 1

    task_id = str(run.state["task_id"])
    while run.interrupt is not None:
        payload = run.interrupt
        print(f"interrupt_type={payload['type']}")
        print(f"interrupt_reason={payload.get('reason')}")
        if payload["type"] == "tool_approval":
            print(f"approval_tool={payload.get('tool_name')}")
            print(
                "approval_arguments_preview="
                + json.dumps(
                    payload.get("arguments_preview"),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            print(f"approval_risk={payload['risk_level']}")
            # 直接读取：Ctrl+C 能终止整个交互 CLI，而非遗留一个等待输入的线程。
            answer = _console_input("Approve? [y/N] ")
            resume = (
                {"decision": "approve"}
                if answer.strip().lower() == "y"
                else {"decision": "reject", "reason": "Rejected from CLI"}
            )
        elif payload["type"] == "ambiguous_tool_recovery":
            print(f"recovery_tool={payload.get('tool_name')}")
            print(
                "recovery_arguments_preview="
                + json.dumps(
                    payload.get("arguments_preview"),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            answer = _console_input("Action may already have run. Retry anyway? [y/N] ")
            resume = (
                {"choice": "retry", "reason": "Retry accepted from CLI"}
                if answer.strip().lower() == "y"
                else {"choice": "abort", "reason": "Aborted from CLI"}
            )
        elif payload["type"] == "human_assistance":
            print(f"required_action={payload.get('required_action')}")
            print(f"resume_condition={payload.get('resume_condition')}")
            answer = _console_input("完成上述操作并准备让 TaskPilot 重新验证了吗？ [y/N] ")
            resume = (
                {"choice": "completed", "reason": "Completed from CLI"}
                if answer.strip().lower() == "y"
                else {"choice": "cancel", "reason": "Canceled from CLI"}
            )
        else:
            print(f"error=unsupported interrupt type: {payload['type']}")
            return 1
        run = await service.resume_task(
            task_id,
            interrupt_type=payload["type"],
            response=resume,
            progress_handler=print_progress_event,
        )
    result = run.state
    status = result["status"]
    status_value = status.value if isinstance(status, TaskStatus) else str(status)
    print(f"task_id={result['task_id']}")
    print(f"status={status_value}")
    step_outcome = result.get("step_outcome")
    if args.verbose:
        trace_summary = await service.get_trace_summary(task_id)
        task_spec = result.get("task_spec")
        if task_spec is not None:
            print(f"goal={task_spec.goal}")
            print(
                "constraints="
                + json.dumps(task_spec.constraints, ensure_ascii=False, sort_keys=True)
            )
            print(f"expected_output={task_spec.expected_output}")
            print(
                "completion_criteria="
                + json.dumps(task_spec.completion_criteria, ensure_ascii=False)
            )
        print("plan:")
        for step in result.get("plan", []):
            print(f"  step_id={step.id}")
            print(f"    description={step.description}")
            print(f"    depends_on={json.dumps(step.depends_on)}")
            print(
                "    success_criteria="
                + json.dumps(step.success_criteria, ensure_ascii=False)
            )
            print(f"    status={step.status.value}")
        print(f"current_step_id={result.get('current_step_id')}")
        print(f"plan_revision={result.get('plan_revision', 0)}")
        print(f"replan_count={result.get('replan_count', 0)}")
        if trace_summary is not None:
            print("trace_summary=" + trace_summary.model_dump_json())
        if step_outcome is not None:
            print(f"claimed_complete={step_outcome.claimed_complete}")
            print(f"action_count={step_outcome.action_count}")
            print(f"execution_summary={step_outcome.summary}")
            print(
                "execution_evidence="
                + json.dumps(step_outcome.evidence, ensure_ascii=False)
            )
            print(
                "execution_output="
                + json.dumps(step_outcome.output, ensure_ascii=False)
            )
            if step_outcome.error:
                print(f"execution_error={step_outcome.error}")
        print(
            "step_results="
            + json.dumps(
                [
                    item.model_dump(mode="json")
                    for item in result.get("step_results", [])
                ],
                ensure_ascii=False,
            )
        )
        verification = result.get("verification")
        if verification is not None:
            print(f"task_verified={verification.completed}")
            print(f"verification_reason={verification.reason}")
    if status_value in {
        TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.PARTIAL.value
    }:
        final_output = render_final_output(result)
        if final_output is not None:
            print("\nfinal_output:")
            print(final_output)
    if result.get("error") and args.verbose:
        print(f"error={result['error']}")
    if result.get("error"):
        return 1
    if step_outcome is not None and step_outcome.error:
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 顶层创建一次事件循环；Graph node 内不会创建 event loop。"""

    configure_console_encoding()
    try:
        return asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        # asyncio.run 取消执行中的 Graph 后会在最外层重抛 KeyboardInterrupt。
        # 这里统一收尾，避免用户主动 Ctrl+C 时出现一整屏异常堆栈。
        print("\n已退出 TaskPilot。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
