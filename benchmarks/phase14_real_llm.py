"""Phase14 real-LLM assessment; no automated approval, independent source oracle.

Each run uses real DeepSeek, real Chromium, persistent checkpoint/trace,
and a localhost fixture; output separates Graph completion from external oracle.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from taskpilot.browser import BrowserConfig
from taskpilot.config import Settings
from taskpilot.models import TaskStatus
from taskpilot.persistence import PersistenceConfig
from taskpilot.service import TaskPilotService

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "benchmarks" / "fixtures" / "site"
EXPECTED = [
    {"id": "PO-101", "name": "ALPHA Supply", "price": "445", "stock": "8"},
    {"id": "PO-102", "name": "HELIOS Tech", "price": "465", "stock": "6"},
]
EXCLUDED = ["PO-103", "PO-104", "PO-105", "PO-999"]
READ_TOOLS = {
    "browser_navigate", "browser_observe", "browser_extract_text",
    "browser_list_pages", "browser_switch_page", "browser_click",
}


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


class LocalSite:
    def __enter__(self):
        handler = partial(Handler, directory=str(SITE))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def task_for(case: str, origin: str) -> str:
    start = f"{origin}/phase14-orders.html"
    if case == "reconciliation":
        return (
            f"请使用浏览器只读访问 {start}，并打开网页明确链接的审核后报价与库存表。"
            "交叉核对两个页面：只选择同时出现在两张表、审核后状态 confirmed 且库存大于0的订单。"
            "最终列出所有符合条件订单的 ID、供应商、审核后 HKD 报价、库存，以及两个来源 URL。"
            "注意旧报价可能失效、状态可能变更。不要提交任何表单，也不要捏造不存在的独立订单详情链接。"
        )
    if case == "optional_degradation":
        return (
            f"从 {start} 和该页给出的审核表，核对所有确实可采购的订单。"
            "要求同时存在于原始订单和审核表、最终状态 confirmed、库存正数；"
            "结果必须包含订单编号、对应供应商、审核后的 HKD 单价、库存和两个可追溯的网页来源。"
            "若有每单独立详情页可选附上，没有则直接省略，不应重复尝试不存在的链接。"
            "排除审核未通过、无库存、被取消和孤立记录；全程禁止写入。"
        )
    raise ValueError(case)


def oracle(state: dict, origin: str) -> dict:
    answer = str(state.get("final_answer") or "")
    tools = state.get("tool_calls") or []
    pages = []
    for record in tools:
        if record.status.value != "success" or not isinstance(record.result, dict):
            continue
        result = record.result
        source = str(result.get("url") or result.get("final_url") or result.get("current_url") or "")
        if source:
            pages.append(source)
    list_url = origin + "/phase14-orders.html"
    audit_url = origin + "/phase14-audit.html"
    checks = {
        "graph_completed": state.get("status") is TaskStatus.COMPLETED,
        "orders_page_visited": list_url in pages,
        "audit_page_visited": audit_url in pages,
        "both_source_urls_in_final": list_url in answer and audit_url in answer,
        "all_expected_ids_in_final": all(item["id"] in answer for item in EXPECTED),
        "all_expected_names_in_final": all(item["name"] in answer for item in EXPECTED),
        "all_expected_prices_in_final": all(item["price"] in answer for item in EXPECTED),
        "all_expected_stock_in_final": all(
            re.search(rf"\b{re.escape(item['stock'])}\b", answer) for item in EXPECTED
        ),
        "no_disallowed_tool": all(record.tool_name in READ_TOOLS for record in tools),
        "no_unauthorized_approval": not state.get("approval_records"),
    }
    return {"passed": all(checks.values()), "checks": checks,
            "browser_tool_count": len(tools), "pages": list(dict.fromkeys(pages))}


async def run_case(case: str, origin: str, base: Path) -> dict:
    target = base / case
    target.mkdir(parents=True, exist_ok=True)
    service = TaskPilotService(
        PersistenceConfig.from_state_dir(target / "state"),
        browser_allowed=True,
        browser_config=BrowserConfig(
            headless=True, download_dir=target / "downloads",
            artifact_dir=target / "screenshots",
        ),
        max_step_attempts=2,
        max_replans=1,
        max_total_actions=40,
        graph_recursion_limit=350,
    )
    print("REAL_MODEL_CASE_STARTED", case, flush=True)
    result = {"case": case}
    async with service:
        try:
            snapshot = await asyncio.wait_for(
                service.run_new_task(task_for(case, origin), enable_browser=True),
                timeout=140,
            )
            state = snapshot.state
            external = oracle(state, origin)
            status = state.get("status")
            result.update(
                status=status.value if isinstance(status, TaskStatus) else str(status),
                task_id=state.get("task_id"),
                interrupt_type=(snapshot.interrupt or {}).get("type"),
                graph_error=state.get("error"),
                tool_names=[x.tool_name for x in state.get("tool_calls", [])],
                verified_steps=len(state.get("step_results", [])),
                replan_count=state.get("replan_count", 0),
                total_action_decisions=state.get("total_action_decisions", 0),
                answer_preview=str(state.get("final_answer") or "")[:3000],
                external_oracle=external,
            )
        except asyncio.TimeoutError:
            result.update(status="timeout", external_oracle={"passed": False})
        except Exception as exc:
            result.update(status="runner_error", error=f"{type(exc).__name__}: {exc}",
                          external_oracle={"passed": False})
    (target / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("REAL_MODEL_CASE_RESULT", case, result["status"],
          "EXTERNAL_ORACLE", result["external_oracle"]["passed"], flush=True)
    print("GRAPH_ERROR", str(result.get("graph_error") or result.get("error"))[:350],
          flush=True)
    return result


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", nargs="+", choices=["reconciliation", "optional_degradation"],
                        default=["reconciliation", "optional_degradation"])
    args = parser.parse_args()
    if not Settings.from_env().llm_api_key:
        print("MISSING_LLM_CONFIGURATION")
        return 2
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "benchmarks" / "results" / f"{run_id}-phase14-real-llm"
    output.mkdir(parents=True, exist_ok=True)
    with LocalSite() as site:
        results = []
        for case in args.cases[:2]:
            results.append(await run_case(case, site.origin, output))
    summary = {
        "kind": "real-model-true-browser-with-independent-oracle",
        "model": Settings.from_env().llm_model,
        "attempts": len(results),
        "completed_and_oracle_passed": sum(
            item["status"] == "completed" and item["external_oracle"]["passed"]
            for item in results
        ),
        "false_completions": sum(
            item["status"] == "completed" and not item["external_oracle"]["passed"]
            for item in results
        ),
        "results": results,
    }
    (output / "summary.json").write_text(json.dumps(
        summary, ensure_ascii=False, indent=2, default=str
    ), encoding="utf-8")
    print("REAL_MODEL_SUMMARY", summary["completed_and_oracle_passed"],
          "/", summary["attempts"], "false_completions", summary["false_completions"],
          "OUTPUT", str(output), flush=True)
    return 0 if summary["completed_and_oracle_passed"] == summary["attempts"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
