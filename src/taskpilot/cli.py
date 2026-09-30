"""TaskPilot 的 async Host/CLI 入口。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from taskpilot.browser import BrowserConfig
from taskpilot.config import Settings, redact_sensitive_values
from taskpilot.mcp.config import load_mcp_server_configs
from taskpilot.models import TaskStatus
from taskpilot.persistence import PersistenceConfig
from taskpilot.service import (
    TaskConflictError,
    TaskNotFoundError,
    TaskPilotService,
    create_initial_state as create_initial_state,
)
from taskpilot.vision import VisionConfig


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
    if args.browser:
        argv.append("--browser")
    if args.headed:
        argv.append("--headed")
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


async def interactive_shell(args: argparse.Namespace) -> int:
    """常驻读取多个独立任务；每条任务沿用同一组 Host 能力参数。"""

    print(startup_banner(args))
    print("输入任务并回车；输入 /help 查看帮助，/exit 退出。")
    while True:
        try:
            raw = await asyncio.to_thread(input, "taskpilot> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
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
            print("\n当前任务已中断，交互 Shell 继续运行。")
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
    args = parser.parse_args(argv)

    if args.headed and not args.browser:
        parser.error("--headed requires --browser")
    if args.interactive:
        if args.task is not None or args.resume is not None:
            parser.error("--interactive cannot be combined with TASK or --resume")
        return await interactive_shell(args)
    if (args.task is None) == (args.resume is None):
        parser.error("provide exactly one of TASK or --resume TASK_ID")

    persistence_config = PersistenceConfig.from_state_dir(args.state_dir)
    mcp_configs = (
        load_mcp_server_configs(args.mcp_config) if args.mcp_config is not None else []
    )
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
                run = await service.continue_task(args.resume)
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
        print(f"approval_tool={payload['tool_name']}")
        print(f"approval_reason={payload['reason']}")
        print(
            "approval_arguments_preview="
            + json.dumps(
                payload["arguments_preview"],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        if payload["type"] == "tool_approval":
            print(f"approval_risk={payload['risk_level']}")
            answer = await asyncio.to_thread(input, "Approve? [y/N] ")
            resume = (
                {"decision": "approve"}
                if answer.strip().lower() == "y"
                else {"decision": "reject", "reason": "Rejected from CLI"}
            )
        elif payload["type"] == "ambiguous_tool_recovery":
            answer = await asyncio.to_thread(
                input,
                "Action may already have run. Retry anyway? [y/N] ",
            )
            resume = (
                {"choice": "retry", "reason": "Retry accepted from CLI"}
                if answer.strip().lower() == "y"
                else {"choice": "abort", "reason": "Aborted from CLI"}
            )
        else:
            print(f"error=unsupported interrupt type: {payload['type']}")
            return 1
        run = await service.resume_task(
            task_id,
            interrupt_type=payload["type"],
            response=resume,
        )
    result = run.state
    trace_summary = await service.get_trace_summary(task_id)
    status = result["status"]
    status_value = status.value if isinstance(status, TaskStatus) else str(status)
    print(f"task_id={result['task_id']}")
    print(f"status={status_value}")
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
    step_outcome = result.get("step_outcome")
    if step_outcome is not None:
        print(f"claimed_complete={step_outcome.claimed_complete}")
        print(f"action_count={step_outcome.action_count}")
        print(f"execution_summary={step_outcome.summary}")
        print(
            "execution_evidence="
            + json.dumps(step_outcome.evidence, ensure_ascii=False)
        )
        print("execution_output=" + json.dumps(step_outcome.output, ensure_ascii=False))
        if step_outcome.error:
            print(f"execution_error={step_outcome.error}")
    print(
        "step_results="
        + json.dumps(
            [item.model_dump(mode="json") for item in result.get("step_results", [])],
            ensure_ascii=False,
        )
    )
    verification = result.get("verification")
    if verification is not None:
        print(f"task_verified={verification.completed}")
        print(f"verification_reason={verification.reason}")
    if status_value == TaskStatus.COMPLETED.value:
        final_output = render_final_output(result)
        if final_output is not None:
            print("\nfinal_output:")
            print(final_output)
    if result.get("error"):
        print(f"error={result['error']}")
        return 1
    if step_outcome is not None and step_outcome.error:
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 顶层创建一次事件循环；Graph node 内不会创建 event loop。"""

    configure_console_encoding()
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
