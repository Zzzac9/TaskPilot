# TaskPilot Phase 8 Review

> 基于 2026-09-01 当前工作区最终代码与实际命令输出生成，面向人工架构和代码验收。

## 1. Phase Goal

本阶段目标是在统一 Tool execution boundary 上加入 deterministic Risk Policy 与真实 LangGraph Human-in-the-loop。实际完成 LOW/MEDIUM/HIGH 分类、ALLOW/REQUIRE_APPROVAL/DENY、Browser click 无副作用 preflight、MCP annotation trust boundary、Local Tool 保守默认、敏感 preview 脱敏、冻结动作审批、拒绝反馈、CLI 审批循环和进程内 MemorySaver。

本阶段没有实现 durable SQLite checkpoint、crash recovery、idempotency ledger、Dynamic Replanning、Vision/OCR、完整 Prompt Injection 防护、FastAPI 或 Benchmark execution；没有开始 Phase 9。

## 2. Final Directory Tree

```text
TaskPilot/
├── pyproject.toml
├── .env.example
├── .gitignore
├── README.md
├── benchmarks/
│   └── README.md
├── docs/
│   └── review/
│       ├── phase_03.md
│       ├── phase_04.md
│       ├── phase_05.md
│       ├── phase_06.md
│       ├── phase_07.md
│       └── phase_08.md
├── src/
│   └── taskpilot/
│       ├── __init__.py
│       ├── analyzer.py
│       ├── browser/
│       │   ├── __init__.py
│       │   ├── config.py
│       │   ├── locators.py
│       │   ├── session.py
│       │   └── tools.py
│       ├── cli.py
│       ├── config.py
│       ├── executor.py
│       ├── graph.py
│       ├── llm.py
│       ├── mcp/
│       │   ├── __init__.py
│       │   ├── adapter.py
│       │   ├── config.py
│       │   └── provider.py
│       ├── models.py
│       ├── planner.py
│       ├── policy/
│       │   ├── __init__.py
│       │   ├── browser.py
│       │   ├── engine.py
│       │   └── models.py
│       ├── state.py
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   └── registry.py
│       └── verifier.py
└── tests/
    ├── conftest.py
    ├── fixtures/
    │   ├── browser_site/
    │   │   ├── delayed.html
    │   │   ├── details.html
    │   │   ├── download.txt
    │   │   ├── frame.html
    │   │   └── index.html
    │   ├── mcp_config.json
    │   └── mcp_server.py
    ├── test_analyzer.py
    ├── test_browser.py
    ├── test_browser_executor.py
    ├── test_browser_policy.py
    ├── test_executor.py
    ├── test_graph_smoke.py
    ├── test_hitl.py
    ├── test_mcp.py
    ├── test_mcp_integration.py
    ├── test_models.py
    ├── test_phase5_graph.py
    ├── test_planner.py
    ├── test_policy.py
    ├── test_tools.py
    └── test_verifier.py
```

`tests/.artifacts`、cache 和 bytecode 为忽略的运行产物，未展开。

## 3. Installed LangGraph Version

实际命令：

```powershell
conda run -n ai python -c "import importlib.metadata, inspect; from langgraph.types import interrupt, Command; from langgraph.checkpoint.memory import MemorySaver; print('langgraph='+importlib.metadata.version('langgraph')); print('interrupt='+interrupt.__module__+'.'+interrupt.__name__+str(inspect.signature(interrupt))); print('Command='+Command.__module__+'.'+Command.__name__); print('checkpointer='+MemorySaver.__module__+'.'+MemorySaver.__name__)"
```

实际输出：

```text
langgraph=0.2.60
interrupt=langgraph.types.interrupt(value: Any) -> Any
Command=langgraph.types.Command
checkpointer=langgraph.checkpoint.memory.MemorySaver
```

当前安装版本是 `langgraph 0.2.60`，没有使用 deprecated `NodeInterrupt`。

## 4. interrupt / Command / Checkpointer Actual API

实际 import：

```python
from langgraph.types import Command, interrupt
from langgraph.checkpoint.memory import MemorySaver
```

编译：

```python
graph = builder.compile(checkpointer=MemorySaver())
```

首次调用与恢复：

```python
config = {"configurable": {"thread_id": task_id}}
paused = await graph.ainvoke(initial_state, config=config)
final = await graph.ainvoke(
    Command(resume={"decision": "approve"}),
    config=config,
)
```

在当前 0.2.60 中，`ainvoke` 的暂停返回值是当前 state，不额外加入 `__interrupt__`。公开 interrupt payload 通过：

```python
snapshot = graph.get_state(config)
interrupts = [
    item
    for task in snapshot.tasks
    for item in task.interrupts
]
payload = interrupts[0].value
```

读取。这是按当前实测 API 选择，不套用新版教程假设。

## 5. Phase 7 Locator Auto-wait Corrective Fix

Phase 7 的 `_strict_locator()` 会在 action 前调用 `locator.count()`，动态元素尚未出现时会立即得到 0，绕过 Locator action 自身的 auto-wait。本阶段改为 `_action_locator()` 只验证并解析 LocatorSpec；`click/fill/select/check/inner_text` 直接交给 Playwright Locator 的 auto-wait、actionability 与 strict mode。

只有无副作用的 click risk preflight 会先 `locator.wait_for(state="attached")`，再确认唯一性并读取 metadata。新增 `delayed.html` 在 500ms 后插入 input/button，测试不使用 sleep，fill/click 成功。

## 6. Risk Architecture

```text
Model external Tool Call
        ↓
ToolRegistry lookup + JSON Schema validation
        ↓
DefaultRiskPolicy.assess (no target action)
        ├ LOW/MEDIUM → ALLOW → registry.invoke
        ├ HIGH → REQUIRE_APPROVAL → PendingToolAction
        └ host block → DENY → error ToolMessage, no invoke

PendingToolAction
        ↓
prepare_approval (WAITING_APPROVAL)
        ↓
human_approval (LangGraph interrupt)
        ├ APPROVE → exact frozen action once → execute_step
        └ REJECT → ApprovalRecord → execute_step alternative
```

Policy 判断“动作是否允许执行”；Verifier 判断执行结果是否满足 Step/Task。两者没有互相代替。

## 7. RiskLevel / PolicyOutcome

`RiskLevel`：LOW、MEDIUM、HIGH。`PolicyOutcome`：ALLOW、REQUIRE_APPROVAL、DENY。默认映射 LOW/MEDIUM → ALLOW，HIGH → REQUIRE_APPROVAL。DENY 只用于显式 Host block。

### `src/taskpilot/policy/models.py`

```python
"""风险判断、待审批动作与人工决定的数据模型。"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RiskLevel(str, Enum):
    """Tool action 的确定性风险等级。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PolicyOutcome(str, Enum):
    """Policy 对动作执行边界的决定。"""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class PolicyDecision(BaseModel):
    """Policy 对一次模型 Tool Call 的 JSON-serializable 判断。"""

    call_id: str
    step_id: int
    tool_name: str
    risk_level: RiskLevel
    outcome: PolicyOutcome
    reason: str
    tool_source: str
    preview: dict[str, Any] = Field(default_factory=dict)
    annotations_trusted: bool | None = None


class PendingToolAction(BaseModel):
    """已经请求但尚未执行、等待 Human Approval 的冻结动作。"""

    call_id: str
    step_id: int
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    policy_decision: PolicyDecision


class ApprovalChoice(str, Enum):
    """Human 对冻结动作仅能批准或拒绝。"""

    APPROVE = "approve"
    REJECT = "reject"


class ApprovalResponse(BaseModel):
    """LangGraph resume value；禁止夹带修改后的 Tool arguments。"""

    model_config = ConfigDict(extra="forbid")

    decision: ApprovalChoice
    reason: str | None = None


class ApprovalRecord(BaseModel):
    """一次 Human Approval 的不可变语义记录。"""

    call_id: str
    step_id: int
    tool_name: str
    decision: ApprovalChoice
    reason: str | None = None
```

## 8. PolicyDecision

`PolicyDecision` 保存 call/step/tool identity、risk、outcome、reason、source、脱敏 preview 和 MCP annotation 是否被 Host 信任。所有字段可 JSON serialization。

## 9. PendingToolAction

`PendingToolAction` 保存模型已经请求但尚未执行的 exact tool name/arguments/call ID 和 PolicyDecision。它不是 ToolCallRecord；审批暂停前不会创建虚假的 PENDING/SUCCESS ToolCallRecord。

## 10. Approval Models

`ApprovalResponse` 使用 Pydantic `extra="forbid"`，只接受 decision 和 optional reason。resume 不能携带 arguments/tool_name/call_id，也没有 edit-before-approve。`ApprovalRecord` 不保存 arguments，因此不会把 secret 复制进 approval history。

## 11. TaskState Changes

新增 `pending_action`、`policy_decisions`、`approval_records`，所有 initial states 已同步。WAITING_APPROVAL 在 prepare node 中正式使用。

### `src/taskpilot/state.py`

```python
"""TaskPilot 使用的 LangGraph 状态结构。"""

from typing import Any, TypedDict

from taskpilot.models import (
    PlanStep,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    TaskStatus,
    ToolCallRecord,
    VerificationResult,
)
from taskpilot.policy.models import ApprovalRecord, PendingToolAction, PolicyDecision


class TaskState(TypedDict):
    """在 TaskPilot 图节点之间传递的结构化任务状态。"""

    task_id: str
    user_input: str
    task_spec: TaskSpec | None
    plan: list[PlanStep]
    current_step_id: int | None
    step_outcome: StepExecutionOutcome | None
    # step_results 只保存通过 Step Verifier 的正式步骤产物。
    step_results: list[StepResult]
    # step_verification 是当前步骤最近一次验证，不等同于任务级 verification。
    step_verification: StepVerificationResult | None
    # tool_calls 保留原始执行记录，便于后续追踪与恢复。
    tool_calls: list[ToolCallRecord]
    # task_results 只保存与最终目标直接相关的提炼结果，不与工具日志混用。
    task_results: dict[str, Any]
    verification: VerificationResult | None
    # pending_action 是未执行的冻结动作，不属于 ToolCallRecord。
    pending_action: PendingToolAction | None
    policy_decisions: list[PolicyDecision]
    approval_records: list[ApprovalRecord]
    status: TaskStatus
    error: str | None


class TaskStateUpdate(TypedDict, total=False):
    """图节点返回的部分状态更新；未返回的字段由 LangGraph 原样保留。"""

    task_id: str
    user_input: str
    task_spec: TaskSpec | None
    plan: list[PlanStep]
    current_step_id: int | None
    step_outcome: StepExecutionOutcome | None
    step_results: list[StepResult]
    step_verification: StepVerificationResult | None
    tool_calls: list[ToolCallRecord]
    task_results: dict[str, Any]
    verification: VerificationResult | None
    pending_action: PendingToolAction | None
    policy_decisions: list[PolicyDecision]
    approval_records: list[ApprovalRecord]
    status: TaskStatus
    error: str | None
```

## 12. DefaultRiskPolicy Full Source

Policy 是 async，因为 Browser dynamic preflight 需要读 DOM。它不调 LLM、不调用目标 Tool、不改 arguments。

### `src/taskpilot/policy/__init__.py`

```python
"""TaskPilot deterministic Tool Risk Policy。"""

from taskpilot.policy.engine import DefaultRiskPolicy, ToolPolicyLike, redact_preview
from taskpilot.policy.models import (
    ApprovalChoice,
    ApprovalRecord,
    ApprovalResponse,
    PendingToolAction,
    PolicyDecision,
    PolicyOutcome,
    RiskLevel,
)

__all__ = [
    "ApprovalChoice",
    "ApprovalRecord",
    "ApprovalResponse",
    "DefaultRiskPolicy",
    "PendingToolAction",
    "PolicyDecision",
    "PolicyOutcome",
    "RiskLevel",
    "ToolPolicyLike",
    "redact_preview",
]
```

### `src/taskpilot/policy/engine.py`

```python
"""统一适用于 Browser、MCP 与 Local Tool 的 deterministic Risk Policy。"""

import re
from typing import Any, Protocol

from taskpilot.policy.browser import classify_browser_click
from taskpilot.policy.models import PolicyDecision, PolicyOutcome, RiskLevel
from taskpilot.tools import Tool


_SENSITIVE_KEY = re.compile(
    r"password|passwd|token|secret|api[_-]?key|authorization|cookie|credential",
    re.IGNORECASE,
)


class ToolPolicyLike(Protocol):
    """真实 Policy 与测试替身共享的异步风险判断接口。"""

    async def assess(
        self,
        *,
        call_id: str,
        step_id: int,
        tool: Tool,
        arguments: dict[str, Any],
    ) -> PolicyDecision:
        """只分类与读取 preflight metadata，不执行目标 Tool。"""

        ...


def redact_preview(value: Any) -> Any:
    """递归脱敏 human-facing preview，不修改真实 Tool arguments。"""

    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _SENSITIVE_KEY.search(str(key))
                else redact_preview(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_preview(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class DefaultRiskPolicy:
    """基于 Host metadata 与无副作用 Browser preflight 的默认策略。"""

    async def assess(
        self,
        *,
        call_id: str,
        step_id: int,
        tool: Tool,
        arguments: dict[str, Any],
    ) -> PolicyDecision:
        metadata = getattr(tool, "metadata", {}) or {}
        source = str(metadata.get("source") or "unknown")
        annotations_trusted: bool | None = None

        if metadata.get("policy_outcome") == PolicyOutcome.DENY.value:
            risk = RiskLevel.HIGH
            outcome = PolicyOutcome.DENY
            reason = str(metadata.get("policy_reason") or "Host policy 明确禁止该 Tool")
        elif source == "browser":
            risk, reason = await self._assess_browser(tool, metadata, arguments)
            outcome = self._outcome_for(risk)
        elif source == "mcp":
            annotations_trusted = bool(metadata.get("trust_tool_annotations", False))
            risk, reason = self._assess_mcp(metadata, annotations_trusted)
            outcome = self._outcome_for(risk)
        else:
            risk, reason = self._assess_local_or_unknown(metadata, source)
            outcome = self._outcome_for(risk)

        preview = redact_preview(arguments)
        if not isinstance(preview, dict):  # pragma: no cover - arguments 固定为 dict
            preview = {"value": preview}
        return PolicyDecision(
            call_id=call_id,
            step_id=step_id,
            tool_name=tool.name,
            risk_level=risk,
            outcome=outcome,
            reason=reason,
            tool_source=source,
            preview=preview,
            annotations_trusted=annotations_trusted,
        )

    @staticmethod
    async def _assess_browser(
        tool: Tool,
        metadata: dict[str, Any],
        arguments: dict[str, Any],
    ) -> tuple[RiskLevel, str]:
        declared = str(metadata.get("risk") or "high")
        if declared != "dynamic":
            try:
                return RiskLevel(declared), "TaskPilot first-party Browser Tool metadata"
            except ValueError:
                return RiskLevel.HIGH, "Browser Tool risk metadata 无法识别"

        preflight = getattr(tool, "preflight", None)
        if preflight is None:
            return RiskLevel.HIGH, "Dynamic Browser Tool 缺少可用 preflight"
        inspected = await preflight(arguments)
        return classify_browser_click(inspected)

    @staticmethod
    def _assess_mcp(
        metadata: dict[str, Any],
        trusted: bool,
    ) -> tuple[RiskLevel, str]:
        if not trusted:
            return (
                RiskLevel.HIGH,
                "MCP annotations 未被 Host 信任，按未知 write-capable Tool 处理",
            )
        annotations = metadata.get("annotations")
        if not isinstance(annotations, dict):
            return RiskLevel.HIGH, "可信 MCP Server 未提供完整 annotations"
        read_only = annotations.get("read_only_hint")
        destructive = annotations.get("destructive_hint")
        open_world = annotations.get("open_world_hint")
        if read_only is True:
            return RiskLevel.LOW, "可信 MCP annotation 声明 read-only"
        if read_only is False and (destructive is True or open_world is True):
            return RiskLevel.HIGH, "可信 MCP annotation 提示 destructive/open-world write"
        if read_only is False and destructive is False and open_world is False:
            return RiskLevel.MEDIUM, "可信 MCP annotation 表示受限、非破坏性写入"
        return RiskLevel.HIGH, "MCP annotations 不完整，采用保守默认"

    @staticmethod
    def _assess_local_or_unknown(
        metadata: dict[str, Any],
        source: str,
    ) -> tuple[RiskLevel, str]:
        declared = metadata.get("risk")
        if source == "local" and declared in {level.value for level in RiskLevel}:
            return RiskLevel(declared), "Host 显式 Local Tool risk metadata"
        return RiskLevel.HIGH, "未知或无 policy metadata 的 Tool 采用保守默认"

    @staticmethod
    def _outcome_for(risk: RiskLevel) -> PolicyOutcome:
        return (
            PolicyOutcome.REQUIRE_APPROVAL
            if risk is RiskLevel.HIGH
            else PolicyOutcome.ALLOW
        )
```

## 13. Browser Metadata

11 个 Browser Tools 增加 first-party metadata：

| Tool | Risk |
|---|---|
| navigate / observe / extract_text / list_pages / switch_page | low |
| fill / select / check / download / screenshot | medium |
| click | dynamic |

BrowserTool 可选 `preflight()` 仅供 dynamic click，Registry/Tool Protocol 没有被强迫增加复杂接口。

## 14. browser_click Preflight Implementation

Preflight 解析相同 LocatorSpec，等待 attached，要求唯一，然后使用公开 `Locator.evaluate` 只读 tag/type/text/value/aria-label/href/form/method/submit metadata；不 click、不修改 DOM。普通 action 不做提前 count。LLM 仍没有 `browser_evaluate` Tool。

### `src/taskpilot/browser/tools.py`

```python
"""DOM-first Playwright Browser Tools 与长生命周期 Provider。"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from taskpilot.browser.config import BrowserConfig
from taskpilot.browser.locators import LOCATOR_JSON_SCHEMA, LocatorSpec, resolve_locator
from taskpilot.browser.session import (
    BrowserDownloadError,
    BrowserError,
    BrowserLocatorError,
    BrowserNavigationError,
    BrowserSession,
    concise_playwright_error,
)
from taskpilot.tools import ToolRegistry


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_LOCATOR_PRIORITY = (
    "Prefer role + accessible name, then label, test_id, text/placeholder, "
    "and use CSS only as a fallback. Strict matching is preserved."
)


@dataclass
class BrowserTool:
    """把一个 Browser handler 暴露为统一 async Tool。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    _handler: Callable[[dict[str, Any]], Awaitable[Any]]
    metadata: dict[str, Any] = field(default_factory=dict)
    _preflight: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None

    async def invoke(self, arguments: dict[str, Any]) -> Any:
        return await self._handler(arguments)

    async def preflight(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """只读取 dynamic action metadata，不执行目标动作。"""

        if self._preflight is None:
            raise BrowserError(f"Browser Tool {self.name} 没有 dynamic preflight")
        return await self._preflight(arguments)


def _object_schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


class BrowserTools:
    """BrowserSession 上的 JSON-serializable Tool handlers。"""

    def __init__(self, session: BrowserSession) -> None:
        self.session = session
        self._screenshot_number = 1

    def build(self) -> list[BrowserTool]:
        page_id = {"type": "string", "minLength": 1}
        locator = LOCATOR_JSON_SCHEMA
        return [
            BrowserTool(
                "browser_navigate",
                "Navigate an existing page to an HTTP(S) URL.",
                _object_schema(
                    {"url": {"type": "string", "minLength": 1}, "page_id": page_id},
                    ["url"],
                ),
                self.navigate,
                metadata={"source": "browser", "risk": "low"},
            ),
            BrowserTool(
                "browser_observe",
                "Return an AI-mode ARIA snapshot, URL, title and stable page IDs.",
                _object_schema(
                    {
                        "page_id": page_id,
                        "depth": {"type": "integer", "minimum": 1, "maximum": 30},
                    }
                ),
                self.observe,
                metadata={"source": "browser", "risk": "low"},
            ),
            BrowserTool(
                "browser_click",
                f"Click one strict semantic locator. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {
                        "locator": locator,
                        "page_id": page_id,
                        "expect_popup": {"type": "boolean", "default": False},
                    },
                    ["locator"],
                ),
                self.click,
                metadata={"source": "browser", "risk": "dynamic"},
                _preflight=self.inspect_click,
            ),
            BrowserTool(
                "browser_fill",
                f"Fill one strict textbox locator. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {
                        "locator": locator,
                        "value": {"type": "string"},
                        "page_id": page_id,
                    },
                    ["locator", "value"],
                ),
                self.fill,
                metadata={"source": "browser", "risk": "medium"},
            ),
            BrowserTool(
                "browser_select",
                f"Select one option by value on a strict locator. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {
                        "locator": locator,
                        "value": {"type": "string"},
                        "page_id": page_id,
                    },
                    ["locator", "value"],
                ),
                self.select,
                metadata={"source": "browser", "risk": "medium"},
            ),
            BrowserTool(
                "browser_check",
                f"Check one checkbox/radio locator. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {"locator": locator, "page_id": page_id},
                    ["locator"],
                ),
                self.check,
                metadata={"source": "browser", "risk": "medium"},
            ),
            BrowserTool(
                "browser_extract_text",
                f"Extract bounded text from one locator or body. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {
                        "locator": locator,
                        "page_id": page_id,
                        "max_chars": {"type": "integer", "minimum": 1},
                    }
                ),
                self.extract_text,
                metadata={"source": "browser", "risk": "low"},
            ),
            BrowserTool(
                "browser_list_pages",
                "List stable page IDs with active state, title and URL.",
                _object_schema({}),
                self.list_pages,
                metadata={"source": "browser", "risk": "low"},
            ),
            BrowserTool(
                "browser_switch_page",
                "Switch the active page using a stable page_id.",
                _object_schema({"page_id": page_id}, ["page_id"]),
                self.switch_page,
                metadata={"source": "browser", "risk": "low"},
            ),
            BrowserTool(
                "browser_download",
                f"Click one locator and save its download only in the controlled directory. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {"locator": locator, "page_id": page_id},
                    ["locator"],
                ),
                self.download,
                metadata={"source": "browser", "risk": "medium"},
            ),
            BrowserTool(
                "browser_screenshot",
                "Save a system-named PNG in the controlled artifact directory; no Vision call.",
                _object_schema(
                    {
                        "page_id": page_id,
                        "full_page": {"type": "boolean", "default": False},
                    }
                ),
                self.screenshot,
                metadata={"source": "browser", "risk": "medium"},
            ),
        ]

    async def navigate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        url = arguments["url"]
        if urlsplit(url).scheme.lower() not in {"http", "https"}:
            raise BrowserNavigationError(
                f"browser_navigate 拒绝非 HTTP(S) URL: {urlsplit(url).scheme or 'missing'}"
            )
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.session.config.navigation_timeout_ms,
            )
            return {
                "page_id": page_id,
                "final_url": page.url,
                "title": await page.title(),
                "status": response.status if response is not None else None,
            }
        except PlaywrightError as exc:
            raise BrowserNavigationError(
                f"browser_navigate failed; page_url={page.url}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc

    async def observe(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        depth = arguments.get("depth", self.session.config.default_snapshot_depth)
        try:
            snapshot = await page.aria_snapshot(mode="ai", depth=depth)
        except PlaywrightError as exc:
            raise BrowserError(
                f"browser_observe failed; page_url={page.url}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc
        original_length = len(snapshot)
        truncated = original_length > self.session.config.max_snapshot_chars
        if truncated:
            snapshot = snapshot[: self.session.config.max_snapshot_chars]
        return {
            "page_id": page_id,
            "url": page.url,
            "title": await page.title(),
            "aria_snapshot": snapshot,
            "snapshot_mode": "ai",
            "depth": depth,
            "truncated": truncated,
            "original_length": original_length,
            "pages": await self.session.list_pages(),
        }

    async def click(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(page, arguments["locator"], "click")
        try:
            if arguments.get("expect_popup", False):
                async with page.expect_popup() as popup_info:
                    await locator.click()
                popup = await popup_info.value
                await popup.wait_for_load_state("domcontentloaded")
                new_page_id = self.session.register_page(popup)
            else:
                await locator.click()
                new_page_id = None
            return {
                "clicked": True,
                "page_id": page_id,
                "current_url": page.url,
                "new_page_id": new_page_id,
            }
        except PlaywrightError as exc:
            raise self._locator_action_error("browser_click", page.url, spec, exc) from exc

    async def fill(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(page, arguments["locator"], "fill")
        try:
            await locator.fill(arguments["value"])
            return {"filled": True, "page_id": page_id, "value": arguments["value"]}
        except PlaywrightError as exc:
            raise self._locator_action_error("browser_fill", page.url, spec, exc) from exc

    async def select(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(page, arguments["locator"], "select")
        try:
            selected = await locator.select_option(value=arguments["value"])
            return {"selected": selected, "page_id": page_id}
        except PlaywrightError as exc:
            raise self._locator_action_error("browser_select", page.url, spec, exc) from exc

    async def check(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(page, arguments["locator"], "check")
        try:
            await locator.check()
            return {"checked": await locator.is_checked(), "page_id": page_id}
        except PlaywrightError as exc:
            raise self._locator_action_error("browser_check", page.url, spec, exc) from exc

    async def extract_text(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        try:
            if "locator" in arguments:
                _, locator = self._action_locator(
                    page, arguments["locator"], "extract_text"
                )
                text = await locator.inner_text()
            else:
                text = await page.locator("body").inner_text()
        except PlaywrightError as exc:
            raise BrowserLocatorError(
                f"browser_extract_text failed; page_url={page.url}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc
        limit = min(
            arguments.get("max_chars", self.session.config.max_extract_chars),
            self.session.config.max_extract_chars,
        )
        original_length = len(text)
        return {
            "page_id": page_id,
            "text": text[:limit],
            "truncated": original_length > limit,
            "original_length": original_length,
        }

    async def list_pages(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"pages": await self.session.list_pages()}

    async def switch_page(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page = self.session.switch_page(arguments["page_id"])
        await page.bring_to_front()
        return {
            "page_id": arguments["page_id"],
            "active": True,
            "url": page.url,
            "title": await page.title(),
        }

    async def download(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(page, arguments["locator"], "download")
        try:
            async with page.expect_download() as download_info:
                await locator.click()
            download = await download_info.value
            suggested = download.suggested_filename
            filename = self._sanitize_filename(suggested)
            destination = self._unique_path(self.session.config.download_dir, filename)
            await download.save_as(destination)
            return {
                "filename": destination.name,
                "saved_path": str(destination),
                "suggested_filename": suggested,
                "page_id": page_id,
            }
        except BrowserDownloadError:
            raise
        except PlaywrightError as exc:
            raise BrowserDownloadError(
                f"browser_download failed; page_url={page.url}; "
                f"strategy={spec.strategy.value}; detail={concise_playwright_error(exc)}"
            ) from exc

    async def screenshot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        filename = f"screenshot_{self._screenshot_number:04d}.png"
        self._screenshot_number += 1
        destination = self._unique_path(self.session.config.artifact_dir, filename)
        try:
            await page.screenshot(
                path=str(destination),
                full_page=arguments.get("full_page", False),
            )
        except PlaywrightError as exc:
            raise BrowserError(
                f"browser_screenshot failed; page_url={page.url}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc
        return {"saved_path": str(destination), "page_id": page_id, "url": page.url}

    async def inspect_click(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """等待目标出现后只读取 click 风险 metadata，不触发 click。"""

        _, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(
            page,
            arguments["locator"],
            "click_preflight",
        )
        try:
            # preflight 可以等待 attached；普通 action 不提前 count，保留 auto-wait。
            await locator.wait_for(state="attached")
            count = await locator.count()
            if count != 1:
                raise BrowserLocatorError(
                    "browser_click preflight 必须唯一定位目标; "
                    f"page_url={page.url}; strategy={spec.strategy.value}; count={count}"
                )
            metadata = await locator.evaluate(
                """element => {
                    const tag = element.tagName.toLowerCase();
                    const form = element.closest("form");
                    const typeValue = "type" in element
                        ? String(element.type || "").toLowerCase()
                        : String(element.getAttribute("type") || "").toLowerCase();
                    const isSubmit =
                        (tag === "button" && typeValue === "submit") ||
                        (tag === "input" && ["submit", "image"].includes(typeValue));
                    return {
                        tag_name: tag,
                        input_type: typeValue,
                        text: String(element.innerText || element.textContent || "").trim().slice(0, 500),
                        value: String(element.value || "").slice(0, 500),
                        aria_label: element.getAttribute("aria-label"),
                        href: tag === "a" ? String(element.href || "") : null,
                        in_form: form !== null,
                        form_method: form ? String(form.method || "get").toLowerCase() : null,
                        is_submit: isSubmit,
                    };
                }"""
            )
        except BrowserLocatorError:
            raise
        except PlaywrightError as exc:
            raise BrowserLocatorError(
                "browser_click preflight failed; "
                f"page_url={page.url}; strategy={spec.strategy.value}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc
        metadata["accessible_name"] = spec.name
        metadata["page_url"] = page.url
        metadata["strategy"] = spec.strategy.value
        return metadata

    def _action_locator(
        self,
        page: Page,
        raw_spec: dict[str, Any],
        action: str,
    ) -> tuple[LocatorSpec, Locator]:
        try:
            spec = LocatorSpec.model_validate(raw_spec)
            locator = resolve_locator(page, spec)
        except Exception as exc:
            if isinstance(exc, BrowserError):
                raise
            raise BrowserLocatorError(
                f"browser_{action} locator invalid; page_url={page.url}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc
        return spec, locator

    @staticmethod
    def _locator_action_error(
        action: str,
        url: str,
        spec: LocatorSpec,
        exc: Exception,
    ) -> BrowserLocatorError:
        return BrowserLocatorError(
            f"{action} failed; page_url={url}; strategy={spec.strategy.value}; "
            f"detail={concise_playwright_error(exc)}"
        )

    @staticmethod
    def _sanitize_filename(suggested: str) -> str:
        basename = Path(suggested).name
        sanitized = _SAFE_FILENAME.sub("_", basename).strip("._")
        if not sanitized or sanitized in {".", ".."}:
            raise BrowserDownloadError("下载 suggested_filename 无法安全保存")
        return sanitized

    @staticmethod
    def _unique_path(directory: Path, filename: str) -> Path:
        directory = directory.resolve()
        candidate = (directory / filename).resolve()
        if candidate.parent != directory:
            raise BrowserDownloadError("Browser artifact path 超出受控目录")
        counter = 1
        while candidate.exists():
            candidate = directory / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
            counter += 1
        return candidate


class BrowserToolProvider:
    """Host 侧 BrowserSession lifecycle 与 Tool 注册，不参与推理。"""

    def __init__(self, config: BrowserConfig | None = None) -> None:
        self.config = config or BrowserConfig()
        self.session = BrowserSession(self.config)
        self._tools: list[BrowserTool] = []

    async def __aenter__(self) -> "BrowserToolProvider":
        await self.session.__aenter__()
        self._tools = BrowserTools(self.session).build()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        self._tools = []
        await self.session.__aexit__(exc_type, exc, traceback)

    def register_tools(self, registry: ToolRegistry) -> list[BrowserTool]:
        if not self.session.is_started:
            raise BrowserError("BrowserToolProvider 必须先进入 async context")
        existing = {tool.name for tool in registry.list_tools()}
        conflicts = sorted(tool.name for tool in self._tools if tool.name in existing)
        if conflicts:
            raise BrowserError(f"Browser Tool 与 Registry 名称冲突: {conflicts}")
        for tool in self._tools:
            registry.register(tool)
        return list(self._tools)

    @property
    def tool_names(self) -> list[str]:
        return [tool.name for tool in self._tools]
```

## 15. Browser Risk Rules

提交语义优先于关键词：button/input 的实际 DOM `type=submit` 为 HIGH。英文 delete/remove/send/submit/purchase/buy/pay/order/confirm order/book/publish/post/upload 和中文删除/移除/发送/提交/购买/支付/下单/确认订单/预订/发布/上传为 HIGH。普通 HTTP(S) link 为 LOW；普通非提交 button/input 为 MEDIUM；无法可靠判断的 dynamic click 保守 HIGH。

### `src/taskpilot/policy/browser.py`

```python
"""Browser click preflight metadata 的确定性风险规则。"""

import re
from typing import Any
from urllib.parse import urlsplit

from taskpilot.policy.models import RiskLevel


_ENGLISH_HIGH_RISK = re.compile(
    r"\b(delete|remove|send|submit|purchase|buy|pay|order|"
    r"confirm\s+order|book|publish|post|upload)\b",
    re.IGNORECASE,
)
_CHINESE_HIGH_RISK = (
    "删除",
    "移除",
    "发送",
    "提交",
    "购买",
    "支付",
    "下单",
    "确认订单",
    "预订",
    "发布",
    "上传",
)


def classify_browser_click(metadata: dict[str, Any]) -> tuple[RiskLevel, str]:
    """优先使用 HTML submit 语义，再检查名称，最后按元素类型分类。"""

    if metadata.get("is_submit"):
        return RiskLevel.HIGH, "目标具有 HTML form submit 语义"

    target_text = " ".join(
        str(metadata.get(key) or "")
        for key in ("accessible_name", "aria_label", "text", "value")
    ).strip()
    if _ENGLISH_HIGH_RISK.search(target_text) or any(
        term in target_text for term in _CHINESE_HIGH_RISK
    ):
        return RiskLevel.HIGH, "目标名称包含外部写入或破坏性动作语义"

    tag_name = str(metadata.get("tag_name") or "").lower()
    href = str(metadata.get("href") or "")
    if tag_name == "a" and urlsplit(href).scheme.lower() in {"http", "https"}:
        return RiskLevel.LOW, "普通 HTTP(S) link navigation"
    if tag_name in {"button", "input"}:
        return RiskLevel.MEDIUM, "普通非提交 DOM control interaction"

    # 无法可靠识别的 dynamic click 采用最保守等级，绝不默认 LOW。
    return RiskLevel.HIGH, "无法可靠判断 dynamic click 的副作用"
```

## 16. MCP Annotation Trust Rules

`MCPServerConfig.trust_tool_annotations=False` 是默认值，只表示 Host 是否采信 Tool annotation 风险 hint，不表示整个 Server 安全。

可信 annotations：

- read_only_hint=True → LOW；
- read_only=False 且 destructive/open_world=True → HIGH；
- read_only=False 且 destructive=False 且 open_world=False → MEDIUM；
- 缺失/不完整 → HIGH。

不可信 annotations：即使远端声称 read-only，也保守 HIGH。Annotations inform policy; they do not enforce remote behavior and may be false.

### `src/taskpilot/mcp/config.py`

```python
"""MCP Server transport 的轻量、安全配置模型。"""

import json
from enum import Enum
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class MCPTransport(str, Enum):
    """Phase 6 支持的非 deprecated MCP transports。"""

    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class MCPServerConfig(BaseModel):
    """单个 MCP Server 的连接配置。"""

    model_config = ConfigDict(hide_input_in_errors=True)

    server_id: str
    transport: MCPTransport
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    # SecretStr 确保 repr 和 model_dump(mode="json") 不泄露显式传入的值。
    env: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    url: str | None = None
    # 只表示 Host 是否采信该 Server 的 Tool annotation 风险提示。
    trust_tool_annotations: bool = False

    @field_validator("server_id")
    @classmethod
    def validate_server_id(cls, value: str) -> str:
        """server_id 去除首尾空白后必须非空。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("server_id 不能为空")
        return normalized

    @model_validator(mode="after")
    def validate_transport_fields(self) -> "MCPServerConfig":
        """确保每种 transport 只使用自己必需的连接字段。"""

        if self.transport is MCPTransport.STDIO:
            if not self.command or not self.command.strip():
                raise ValueError("STDIO MCP Server 必须提供 command")
            if self.url is not None:
                raise ValueError("STDIO MCP Server 不能提供 url")
        else:
            if not self.url or not self.url.startswith(("http://", "https://")):
                raise ValueError(
                    "Streamable HTTP MCP Server 必须提供 http(s) url"
                )
            if self.command is not None or self.args or self.env:
                raise ValueError(
                    "Streamable HTTP MCP Server 不能提供 command、args 或 env"
                )
        return self

    def resolved_env(self) -> dict[str, str]:
        """只解开用户显式配置的环境变量，不复制整个 os.environ。"""

        return {
            name: secret.get_secret_value() for name, secret in self.env.items()
        }


class MCPConfigFile(BaseModel):
    """CLI MCP JSON 文件的顶层结构。"""

    servers: list[MCPServerConfig]


def load_mcp_server_configs(path: Path) -> list[MCPServerConfig]:
    """从 JSON 文件读取 MCP Server 配置。"""

    raw = json.loads(path.read_text(encoding="utf-8"))
    config_file = MCPConfigFile.model_validate(raw)
    return config_file.servers
```

### `src/taskpilot/mcp/provider.py`

```python
"""多个 MCP Server 的长生命周期连接、发现与注册。"""

from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Sequence

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import PaginatedRequestParams, Tool as MCPTool

from taskpilot.config import redact_sensitive_values
from taskpilot.mcp.adapter import MCPToolAdapter, build_public_tool_name
from taskpilot.mcp.config import MCPServerConfig, MCPTransport
from taskpilot.tools import ToolRegistry


class MCPConfigurationError(ValueError):
    """MCP 配置、namespace 或注册冲突。"""


class MCPConnectionError(RuntimeError):
    """MCP transport 建连或 initialize 失败。"""


@dataclass(frozen=True)
class MCPDiscoveryInfo:
    """供检查与测试使用的发现映射，不参与 Tool 选择。"""

    server_id: str
    remote_tool_name: str
    public_tool_name: str


class MCPToolProvider:
    """Host 侧 MCP 连接基础设施；不参与 Graph、Planner 或 Verifier。"""

    def __init__(self, configs: Sequence[MCPServerConfig]) -> None:
        self._configs = list(configs)
        self._validate_server_ids()
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, ClientSession] = {}
        self._adapters: list[MCPToolAdapter] = []
        self._discovery_info: list[MCPDiscoveryInfo] = []

    @property
    def connected_server_ids(self) -> list[str]:
        """返回当前仍处于 provider lifecycle 内的 server IDs。"""

        return list(self._sessions)

    @property
    def discovery_info(self) -> list[MCPDiscoveryInfo]:
        """返回动态发现产生的 namespace 映射副本。"""

        return list(self._discovery_info)

    async def __aenter__(self) -> "MCPToolProvider":
        """一次连接并 initialize 所有配置的 MCP Servers。"""

        if self._stack is not None:
            raise RuntimeError("MCPToolProvider 不能重复进入 context")
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            for config in self._configs:
                try:
                    await self._connect(config)
                except Exception as exc:
                    detail = redact_sensitive_values(str(exc))
                    for secret in config.resolved_env().values():
                        if secret:
                            detail = detail.replace(secret, "[REDACTED]")
                    raise MCPConnectionError(
                        f"MCP Server {config.server_id} 连接或 initialize 失败: "
                        f"{type(exc).__name__}: {detail}"
                    ) from exc
        except BaseException:
            await self._stack.aclose()
            self._stack = None
            self._sessions.clear()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool | None:
        """按 LIFO 顺序统一关闭 session 与 transport/subprocess。"""

        stack = self._stack
        self._stack = None
        self._sessions.clear()
        if stack is None:
            return None
        return await stack.__aexit__(exc_type, exc, traceback)

    async def discover_tools(self) -> list[MCPToolAdapter]:
        """完整分页调用 list_tools，并创建 TaskPilot adapters。"""

        if self._stack is None:
            raise RuntimeError("discover_tools 必须在 MCPToolProvider context 内调用")
        if self._adapters:
            return list(self._adapters)

        adapters: list[MCPToolAdapter] = []
        public_names: set[str] = set()
        discovery_info: list[MCPDiscoveryInfo] = []
        for config in self._configs:
            session = self._sessions[config.server_id]
            for remote_tool in await self._list_all_tools(session):
                public_name = build_public_tool_name(
                    config.server_id,
                    remote_tool.name,
                )
                if public_name in public_names:
                    raise MCPConfigurationError(
                        "MCP Tool namespace 冲突: "
                        f"public_tool_name={public_name}"
                    )
                public_names.add(public_name)
                adapters.append(
                    self._make_adapter(
                        config,
                        public_name,
                        remote_tool,
                        session,
                    )
                )
                discovery_info.append(
                    MCPDiscoveryInfo(
                        server_id=config.server_id,
                        remote_tool_name=remote_tool.name,
                        public_tool_name=public_name,
                    )
                )

        self._adapters = adapters
        self._discovery_info = discovery_info
        return list(adapters)

    async def register_tools(self, registry: ToolRegistry) -> list[MCPToolAdapter]:
        """发现并注册 adapters；不会静默覆盖 Local 或其他 MCP Tool。"""

        adapters = await self.discover_tools()
        existing_names = {tool.name for tool in registry.list_tools()}
        conflicts = sorted(
            adapter.name for adapter in adapters if adapter.name in existing_names
        )
        if conflicts:
            raise MCPConfigurationError(
                f"MCP Tool 与现有 Registry 名称冲突: {conflicts}"
            )
        for adapter in adapters:
            registry.register(adapter)
        return adapters

    async def _connect(self, config: MCPServerConfig) -> None:
        """根据当前 MCP SDK v2 transport API 建立并初始化 session。"""

        if self._stack is None:  # pragma: no cover - 仅防御错误内部调用
            raise RuntimeError("MCPToolProvider context 尚未进入")
        if config.transport is MCPTransport.STDIO:
            parameters = StdioServerParameters(
                command=config.command or "",
                args=config.args,
                env=config.resolved_env(),
            )
            streams = await self._stack.enter_async_context(
                stdio_client(parameters)
            )
        else:
            streams = await self._stack.enter_async_context(
                streamable_http_client(config.url or "")
            )
        session = await self._stack.enter_async_context(ClientSession(*streams))
        await session.initialize()
        self._sessions[config.server_id] = session

    @staticmethod
    async def _list_all_tools(session: ClientSession) -> list[MCPTool]:
        """跟随 next_cursor 直到 MCP Server 的最后一页。"""

        tools: list[MCPTool] = []
        cursor: str | None = None
        while True:
            params = (
                PaginatedRequestParams(cursor=cursor)
                if cursor is not None
                else None
            )
            page = await session.list_tools(params=params)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if cursor is None:
                return tools

    @staticmethod
    def _make_adapter(
        config: MCPServerConfig,
        public_name: str,
        remote_tool: MCPTool,
        session: ClientSession,
    ) -> MCPToolAdapter:
        """保留 SDK 实际暴露的 metadata，不执行 Phase 8 风险策略。"""

        metadata = {
            "source": "mcp",
            "trust_tool_annotations": config.trust_tool_annotations,
            "title": remote_tool.title,
            "annotations": (
                remote_tool.annotations.model_dump(mode="json", by_alias=False)
                if remote_tool.annotations is not None
                else None
            ),
            "output_schema": remote_tool.output_schema,
            "icons": (
                [icon.model_dump(mode="json", by_alias=False) for icon in remote_tool.icons]
                if remote_tool.icons is not None
                else None
            ),
            "execution": (
                remote_tool.execution.model_dump(mode="json", by_alias=False)
                if remote_tool.execution is not None
                else None
            ),
            "meta": remote_tool.meta,
        }
        return MCPToolAdapter(
            name=public_name,
            remote_tool_name=remote_tool.name,
            server_id=config.server_id,
            description=remote_tool.description or remote_tool.title or "",
            input_schema=remote_tool.input_schema,
            session=session,
            metadata=metadata,
        )

    def _validate_server_ids(self) -> None:
        server_ids = [config.server_id for config in self._configs]
        if len(server_ids) != len(set(server_ids)):
            raise MCPConfigurationError("MCP server_id 必须唯一")
        namespace_ids = [
            build_public_tool_name(config.server_id, "tool").rsplit("__", 1)[0]
            for config in self._configs
        ]
        if len(namespace_ids) != len(set(namespace_ids)):
            raise MCPConfigurationError(
                "MCP server_id 清理后产生 namespace 冲突"
            )
```

## 17. Local Tool Default Rule

Local Tool 可用 `metadata={"source":"local","risk":"low|medium|high"}` 明确声明。缺失 metadata、未知 source 或未知 risk 都保守 HIGH；Policy 不按测试工具名称写特判。

## 18. Sensitive Argument Redaction

`redact_preview` 递归处理 dict/list，key 命中 password/passwd/token/secret/api_key/authorization/cookie/credential 时替换为 `[REDACTED]`。PendingToolAction 内仍保存原始 arguments；approved execution 使用原始冻结值，脱敏不会修改执行参数。

## 19. Executor Policy Gate

Executor 在 call ID、Tool 存在性和 JSON Schema 都通过后才 assess。PolicyDecision identity 必须与请求完全一致。ALLOW 正常执行；DENY 只形成 error ToolMessage；REQUIRE_APPROVAL 立即返回 pending，不调用 Registry。旧测试可用 `policy=None` 保持 Phase 7 行为；CLI 启用 Browser/MCP 时默认创建 DefaultRiskPolicy。

Approved action 有独立入口，重新确定性校验 schema，但不重新问 LLM、不重新 assess。普通执行错误记录 FAILED ToolCallRecord，并允许 Executor 改走替代方案。

### `src/taskpilot/tools/registry.py`

```python
"""本地 ToolRegistry 与 JSON Schema 参数校验。"""

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from taskpilot.tools.base import Tool, tool_to_function_schema


class ToolRegistryError(ValueError):
    """ToolRegistry 可预期配置或调用错误的基类。"""


class DuplicateToolError(ToolRegistryError):
    """注册重复工具名称时抛出。"""


class UnknownToolError(ToolRegistryError):
    """请求未注册工具时抛出。"""


class ToolArgumentsError(ToolRegistryError):
    """工具参数不符合 input_schema 时抛出。"""


class ToolRegistry:
    """保存可用工具并负责调用前的确定性参数校验。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册名称唯一且 input_schema 合法的工具。"""

        if tool.name == "finish_step":
            raise DuplicateToolError("finish_step 是 Executor 内部控制动作，不能注册")
        if tool.name in self._tools:
            raise DuplicateToolError(f"工具名称已注册: {tool.name}")
        try:
            Draft202012Validator.check_schema(tool.input_schema)
        except SchemaError as exc:
            raise ToolRegistryError(
                f"工具 {tool.name} 的 input_schema 无效: {exc.message}"
            ) from exc
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """按名称返回工具；未知名称会明确失败。"""

        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownToolError(f"未注册的工具: {name}") from exc

    def list_tools(self) -> list[Tool]:
        """按注册顺序返回工具列表副本。"""

        return list(self._tools.values())

    def list_function_schemas(self) -> list[dict[str, Any]]:
        """返回所有工具的 OpenAI-compatible Function schemas。"""

        return [tool_to_function_schema(tool) for tool in self._tools.values()]

    async def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        """校验参数后异步调用工具，不吞掉工具自身异常。"""

        tool = self.get(name)
        self.validate_arguments(name, arguments)
        return await tool.invoke(arguments)

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> None:
        """只做确定性 schema 校验，不执行 Tool。"""

        tool = self.get(name)
        try:
            Draft202012Validator(tool.input_schema).validate(arguments)
        except ValidationError as exc:
            path = ".".join(str(part) for part in exc.absolute_path)
            location = f" at {path}" if path else ""
            raise ToolArgumentsError(
                f"工具 {name} 参数不符合 input_schema{location}: {exc.message}"
            ) from exc
```

### `src/taskpilot/executor.py`

```python
"""单个 PlanStep 的有界 Function Calling Executor Runtime。"""

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from taskpilot.config import redact_sensitive_values
from taskpilot.llm import create_chat_model
from taskpilot.models import (
    PlanStep,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    ToolCallRecord,
    ToolCallStatus,
)
from taskpilot.policy import (
    ApprovalChoice,
    ApprovalRecord,
    PendingToolAction,
    PolicyDecision,
    PolicyOutcome,
    ToolPolicyLike,
)
from taskpilot.tools.registry import (
    ToolArgumentsError,
    ToolRegistry,
    UnknownToolError,
)


FINISH_STEP_NAME = "finish_step"
FINISH_STEP_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": FINISH_STEP_NAME,
        "description": (
            "Declare that the current step has sufficient evidence for its "
            "success criteria. This is a claim, not verifier approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Concise summary of what was achieved.",
                },
                "evidence": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Evidence supporting each step success criterion.",
                },
                "output": {
                    "type": "object",
                    "description": (
                        "Structured result produced by this step for verified "
                        "downstream handoff."
                    ),
                },
            },
            "required": ["summary", "evidence", "output"],
            "additionalProperties": False,
        },
    },
}

EXECUTOR_SYSTEM_PROMPT = """你是 Task Executor，只执行当前 PlanStep，不执行未来步骤，也不修改 Task Goal 或 Constraints。
根据当前环境 Observation 决定下一动作；优先使用可用 Tool 获取事实，不得虚构外部结果。
每轮必须且最多选择一个动作：调用一个外部 Tool，或调用内部 finish_step。
Tool 失败后可以根据错误 Observation 调整下一动作，单次 Tool 失败不等于 Step 或 Task 失败。
只有当前 Step 的 success_criteria 已有充分 evidence 时才能调用 finish_step。
finish_step 的 output 必须只包含本步骤实际得到、可交给依赖步骤的结构化结果。
finish_step 只表示 Executor 声称当前 Step 已具备完成条件，最终是否完成由未来 Verifier 判断。
不要直接把 PlanStep 或整个 Task 声称为 completed。"""


@dataclass(frozen=True)
class ExecutorRunResult:
    """TaskExecutor 返回给 Graph 的步骤结果与新增工具记录。"""

    outcome: StepExecutionOutcome | None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    pending_action: PendingToolAction | None = None
    policy_decisions: list[PolicyDecision] = field(default_factory=list)
    fatal_error: bool = False


@dataclass(frozen=True)
class ApprovedActionResult:
    """冻结动作获批后单次执行的记录；不重新询问模型或 Policy。"""

    tool_call: ToolCallRecord | None = None
    fatal_error: bool = False
    error: str | None = None


class TaskExecutorLike(Protocol):
    """真实 Executor 与离线 Fake 共同遵循的最小接口。"""

    async def execute(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult] = (),
        verification_feedback: StepVerificationResult | None = None,
        policy_decisions: Sequence[PolicyDecision] = (),
        approval_records: Sequence[ApprovalRecord] = (),
    ) -> ExecutorRunResult:
        """只执行传入的当前步骤。"""

        ...

    async def execute_approved_action(
        self,
        *,
        pending_action: PendingToolAction,
        approval_records: Sequence[ApprovalRecord],
    ) -> ApprovedActionResult:
        """只执行已获批的 exact frozen action。"""

        ...


def build_executor_messages(
    *,
    task_spec: TaskSpec,
    step: PlanStep,
    task_results: dict[str, Any],
    tool_calls: Sequence[ToolCallRecord],
    step_results: Sequence[StepResult] = (),
    verification_feedback: StepVerificationResult | None = None,
    policy_decisions: Sequence[PolicyDecision] = (),
    approval_records: Sequence[ApprovalRecord] = (),
) -> list[BaseMessage]:
    """只使用当前步骤执行所需的信息构造模型上下文。"""

    observations = [
        {
            "call_id": record.call_id,
            "tool_name": record.tool_name,
            "status": record.status.value,
            "result": record.result,
            "error": record.error,
        }
        for record in tool_calls
        if record.step_id == step.id
    ]
    dependency_results = [
        result.model_dump(mode="json")
        for result in step_results
        if result.step_id in step.depends_on
    ]
    feedback = (
        verification_feedback.model_dump(mode="json")
        if verification_feedback is not None
        and verification_feedback.step_id == step.id
        else None
    )
    current_policy = [
        decision.model_dump(mode="json")
        for decision in policy_decisions
        if decision.step_id == step.id
    ]
    current_approvals = [
        record.model_dump(mode="json")
        for record in approval_records
        if record.step_id == step.id
    ]
    context = {
        "task_goal": task_spec.goal,
        "task_constraints": task_spec.constraints,
        "current_step": {
            "id": step.id,
            "description": step.description,
            "success_criteria": step.success_criteria,
        },
        "current_step_observations": observations,
        "task_results": task_results,
        "dependency_results": dependency_results,
        "verification_feedback": feedback,
        # Policy preview 已递归脱敏；ApprovalRecord 本身不保存 arguments。
        "policy_feedback": current_policy,
        "approval_history": current_approvals,
    }
    return [
        SystemMessage(content=EXECUTOR_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(context, ensure_ascii=False, default=str)),
    ]


class TaskExecutor:
    """使用原生 Tool Calling 反复执行当前步骤，直到 finish_step 或动作上限。"""

    def __init__(
        self,
        registry: ToolRegistry,
        model: BaseChatModel | None = None,
        max_actions_per_step: int = 5,
        policy: ToolPolicyLike | None = None,
    ) -> None:
        if max_actions_per_step < 1:
            raise ValueError("max_actions_per_step 必须至少为 1")
        self._registry = registry
        self._model = model
        self._max_actions_per_step = max_actions_per_step
        self._policy = policy

    async def execute(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult] = (),
        verification_feedback: StepVerificationResult | None = None,
        policy_decisions: Sequence[PolicyDecision] = (),
        approval_records: Sequence[ApprovalRecord] = (),
    ) -> ExecutorRunResult:
        """运行当前 Step 的单动作循环，不直接修改 PlanStep 状态。"""

        if not self._registry.list_tools():
            return self._error_result(
                step_id=step.id,
                action_count=0,
                error="Executor 当前没有注册任何外部工具",
                fatal_error=True,
            )

        if self._model is None:
            self._model = create_chat_model()
        schemas = self._registry.list_function_schemas() + [FINISH_STEP_SCHEMA]
        model_with_tools = self._model.bind_tools(schemas)
        messages = build_executor_messages(
            task_spec=task_spec,
            step=step,
            task_results=task_results,
            tool_calls=tool_calls,
            step_results=step_results,
            verification_feedback=verification_feedback,
            policy_decisions=policy_decisions,
            approval_records=approval_records,
        )
        new_records: list[ToolCallRecord] = []
        new_decisions: list[PolicyDecision] = []
        seen_call_ids = (
            {record.call_id for record in tool_calls}
            | {record.call_id for record in approval_records}
            | {decision.call_id for decision in policy_decisions}
        )

        for action_index in range(self._max_actions_per_step):
            action_count = action_index + 1
            try:
                response = await model_with_tools.ainvoke(messages)
            except Exception as exc:
                error_detail = redact_sensitive_values(str(exc))
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=(
                        "Executor 模型调用失败: "
                        f"{type(exc).__name__}: {error_detail}"
                    ),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )
            if not isinstance(response, AIMessage):
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error="Executor 模型未返回 AIMessage",
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )

            actions = response.tool_calls
            if len(actions) != 1:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=(
                        "Executor 每轮必须返回且只能返回一个 Tool Call; "
                        f"实际数量: {len(actions)}"
                    ),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )

            action = actions[0]
            action_name = action["name"]
            arguments = action["args"]
            call_id = action.get("id")
            if not call_id:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error="Executor Tool Call 缺少 tool_call_id",
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )
            if call_id in seen_call_ids:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=f"Executor Tool Call ID 重复: {call_id}",
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )
            seen_call_ids.add(call_id)

            if action_name == FINISH_STEP_NAME:
                finish_error = self._validate_finish_arguments(arguments)
                if finish_error is not None:
                    return self._error_result(
                        step_id=step.id,
                        action_count=action_count,
                        error=finish_error,
                        tool_calls=new_records,
                        policy_decisions=new_decisions,
                        fatal_error=True,
                    )
                return ExecutorRunResult(
                    outcome=StepExecutionOutcome(
                        step_id=step.id,
                        claimed_complete=True,
                        summary=arguments["summary"],
                        evidence=arguments["evidence"],
                        output=arguments["output"],
                        action_count=action_count,
                    ),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                )

            try:
                tool = self._registry.get(action_name)
            except UnknownToolError as exc:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=str(exc),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )

            try:
                self._registry.validate_arguments(action_name, arguments)
            except ToolArgumentsError as exc:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=str(exc),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )

            if self._policy is not None:
                try:
                    decision = await self._policy.assess(
                        call_id=call_id,
                        step_id=step.id,
                        tool=tool,
                        arguments=arguments,
                    )
                except Exception as exc:
                    error_detail = redact_sensitive_values(str(exc))
                    return self._error_result(
                        step_id=step.id,
                        action_count=action_count,
                        error=(
                            "Tool Risk Policy 评估失败，动作未执行: "
                            f"{type(exc).__name__}: {error_detail}"
                        ),
                        tool_calls=new_records,
                        policy_decisions=new_decisions,
                        fatal_error=True,
                    )
                if (
                    decision.call_id != call_id
                    or decision.step_id != step.id
                    or decision.tool_name != action_name
                ):
                    return self._error_result(
                        step_id=step.id,
                        action_count=action_count,
                        error="Tool Risk Policy 返回的 action identity 不一致，动作未执行",
                        tool_calls=new_records,
                        policy_decisions=new_decisions,
                        fatal_error=True,
                    )
                new_decisions.append(decision)
                if decision.outcome is PolicyOutcome.REQUIRE_APPROVAL:
                    return ExecutorRunResult(
                        outcome=None,
                        tool_calls=new_records,
                        pending_action=PendingToolAction(
                            call_id=call_id,
                            step_id=step.id,
                            tool_name=action_name,
                            arguments=dict(arguments),
                            policy_decision=decision,
                        ),
                        policy_decisions=new_decisions,
                    )
                if decision.outcome is PolicyOutcome.DENY:
                    messages.append(response)
                    messages.append(
                        ToolMessage(
                            content=json.dumps(
                                {
                                    "status": "denied",
                                    "policy_decision": decision.model_dump(
                                        mode="json"
                                    ),
                                },
                                ensure_ascii=False,
                            ),
                            tool_call_id=call_id,
                            name=action_name,
                            status="error",
                        )
                    )
                    continue

            try:
                result = await self._registry.invoke(action_name, arguments)
            except ToolArgumentsError as exc:  # pragma: no cover - 已预校验
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=str(exc),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )
            except Exception as exc:
                error = redact_sensitive_values(str(exc))
                record = ToolCallRecord(
                    call_id=call_id,
                    step_id=step.id,
                    tool_name=action_name,
                    arguments=arguments,
                    status=ToolCallStatus.FAILED,
                    error=f"{type(exc).__name__}: {error}",
                )
                observation = {
                    "status": "failed",
                    "error": record.error,
                }
                tool_message_status = "error"
            else:
                record = ToolCallRecord(
                    call_id=call_id,
                    step_id=step.id,
                    tool_name=action_name,
                    arguments=arguments,
                    status=ToolCallStatus.SUCCESS,
                    result=result,
                )
                observation = {"status": "success", "result": result}
                tool_message_status = "success"

            new_records.append(record)
            messages.append(response)
            messages.append(
                ToolMessage(
                    content=json.dumps(observation, ensure_ascii=False, default=str),
                    tool_call_id=call_id,
                    name=action_name,
                    status=tool_message_status,
                )
            )

        return self._error_result(
            step_id=step.id,
            action_count=self._max_actions_per_step,
            error=(
                "Executor action limit reached: "
                f"{self._max_actions_per_step}"
            ),
            tool_calls=new_records,
            policy_decisions=new_decisions,
            fatal_error=False,
        )

    async def execute_approved_action(
        self,
        *,
        pending_action: PendingToolAction,
        approval_records: Sequence[ApprovalRecord],
    ) -> ApprovedActionResult:
        """执行一次冻结动作；不重新调用 LLM 或 Risk Policy。"""

        matching = [
            record
            for record in approval_records
            if record.call_id == pending_action.call_id
            and record.step_id == pending_action.step_id
            and record.tool_name == pending_action.tool_name
        ]
        if len(matching) != 1 or matching[0].decision is not ApprovalChoice.APPROVE:
            return ApprovedActionResult(
                fatal_error=True,
                error="冻结 Tool action 缺少唯一 APPROVE record",
            )
        try:
            self._registry.get(pending_action.tool_name)
            self._registry.validate_arguments(
                pending_action.tool_name,
                pending_action.arguments,
            )
        except (UnknownToolError, ToolArgumentsError) as exc:
            return ApprovedActionResult(fatal_error=True, error=str(exc))

        try:
            result = await self._registry.invoke(
                pending_action.tool_name,
                pending_action.arguments,
            )
        except Exception as exc:
            detail = redact_sensitive_values(str(exc))
            record = ToolCallRecord(
                call_id=pending_action.call_id,
                step_id=pending_action.step_id,
                tool_name=pending_action.tool_name,
                arguments=pending_action.arguments,
                status=ToolCallStatus.FAILED,
                error=f"{type(exc).__name__}: {detail}",
            )
        else:
            record = ToolCallRecord(
                call_id=pending_action.call_id,
                step_id=pending_action.step_id,
                tool_name=pending_action.tool_name,
                arguments=pending_action.arguments,
                status=ToolCallStatus.SUCCESS,
                result=result,
            )
        return ApprovedActionResult(tool_call=record)

    @staticmethod
    def _validate_finish_arguments(arguments: dict[str, Any]) -> str | None:
        parameters = FINISH_STEP_SCHEMA["function"]["parameters"]
        try:
            Draft202012Validator(parameters).validate(arguments)
        except ValidationError as exc:
            return f"finish_step 参数无效: {exc.message}"
        return None

    @staticmethod
    def _error_result(
        *,
        step_id: int,
        action_count: int,
        error: str,
        tool_calls: list[ToolCallRecord] | None = None,
        policy_decisions: list[PolicyDecision] | None = None,
        fatal_error: bool,
    ) -> ExecutorRunResult:
        return ExecutorRunResult(
            outcome=StepExecutionOutcome(
                step_id=step_id,
                claimed_complete=False,
                action_count=action_count,
                error=redact_sensitive_values(error),
            ),
            tool_calls=tool_calls or [],
            policy_decisions=policy_decisions or [],
            fatal_error=fatal_error,
        )
```

## 20. Graph HITL Flow

当前实际 Graph：

```text
START → initialize_task → analyze_task → plan_task → execute_step
execute_step
├ fatal → END
├ outcome → verify_step → advance/retry → verify_task → END
└ pending → prepare_approval → human_approval [interrupt]
                              ├ approve → execute_approved_action → execute_step
                              └ reject → handle_rejection → execute_step
```

Browser/MCP lifecycle 仍在 Graph 外；新增的是通用 approval control flow，不是 Browser infrastructure node。

### `src/taskpilot/graph.py`

```python
"""TaskPilot Phase 5 的 LangGraph 验证与步骤推进工作流。"""

from functools import partial
from typing import Any, Literal, Sequence

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from taskpilot.analyzer import TaskAnalyzer, TaskAnalyzerLike
from taskpilot.config import redact_sensitive_values
from taskpilot.executor import ExecutorRunResult, TaskExecutor, TaskExecutorLike
from taskpilot.models import (
    PlanStep,
    PlanStepStatus,
    StepExecutionOutcome,
    StepResult,
    TaskStatus,
)
from taskpilot.planner import TaskPlanner, TaskPlannerLike
from taskpilot.policy import ApprovalChoice, ApprovalRecord, ApprovalResponse
from taskpilot.state import TaskState, TaskStateUpdate
from taskpilot.tools.registry import ToolRegistry
from taskpilot.verifier import (
    TaskVerifier,
    TaskVerifierLike,
    incomplete_outcome_rejection,
    validate_step_verification,
    validate_task_verification,
)


def initialize_task(state: TaskState) -> TaskStateUpdate:
    """将新创建的任务推进到运行状态。"""

    # 只允许 created → running，避免意外覆盖完成、失败等其他状态。
    if state["status"] == TaskStatus.CREATED:
        return {"status": TaskStatus.RUNNING}
    return {}


def analyze_task(
    state: TaskState,
    *,
    analyzer: TaskAnalyzerLike,
) -> TaskStateUpdate:
    """调用 Analyzer，并将结构化结果写回任务状态。"""

    try:
        task_spec = analyzer.analyze(state["user_input"])
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        return {
            "status": TaskStatus.FAILED,
            "error": (
                f"Task Analyzer 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }
    return {"task_spec": task_spec, "error": None}


def plan_task(
    state: TaskState,
    *,
    planner: TaskPlannerLike,
) -> TaskStateUpdate:
    """调用 Planner，并选出第一条依赖已满足的待执行步骤。"""

    task_spec = state["task_spec"]
    if task_spec is None:
        return {
            "status": TaskStatus.FAILED,
            "error": state["error"] or "Task Planner 无法运行: task_spec 缺失",
        }

    try:
        task_plan = planner.plan(task_spec)
        current_step_id = find_next_executable_step(task_plan.steps)
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        return {
            "plan": [],
            "current_step_id": None,
            "status": TaskStatus.FAILED,
            "error": (
                f"Task Planner 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }

    if current_step_id is None:
        return {
            "plan": task_plan.steps,
            "current_step_id": None,
            "status": TaskStatus.FAILED,
            "error": "Task Planner 返回的计划没有可执行步骤",
        }
    return {
        "plan": task_plan.steps,
        "current_step_id": current_step_id,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def execute_step(
    state: TaskState,
    *,
    executor: TaskExecutorLike,
) -> TaskStateUpdate:
    """只执行 current_step_id 对应步骤，并保存 Executor 声明。"""

    current_step_id = state["current_step_id"]
    if current_step_id is None:
        return _failed_update(state["error"] or "Executor 无法运行: current_step_id 缺失")
    task_spec = state["task_spec"]
    if task_spec is None:
        return _failed_update(state["error"] or "Executor 无法运行: task_spec 缺失")

    step_index = _find_unique_step_index(state["plan"], current_step_id)
    if step_index is None:
        return _failed_update(
            "Executor 无法定位唯一当前步骤: "
            f"current_step_id={current_step_id}"
        )

    current_step = state["plan"][step_index]
    if current_step.status not in {
        PlanStepStatus.PENDING,
        PlanStepStatus.RUNNING,
    }:
        return _failed_update(
            f"Executor 不能执行状态为 {current_step.status.value} 的步骤 "
            f"{current_step.id}"
        )

    running_step = current_step.model_copy(
        update={"status": PlanStepStatus.RUNNING}
    )
    updated_plan = list(state["plan"])
    updated_plan[step_index] = running_step
    prior_step_tool_calls = [
        record
        for record in state["tool_calls"]
        if record.step_id == current_step_id
    ]

    try:
        execution = await executor.execute(
            task_spec=task_spec,
            step=running_step,
            task_results=state["task_results"],
            tool_calls=prior_step_tool_calls,
            step_results=state["step_results"],
            verification_feedback=state.get("step_verification"),
            policy_decisions=state.get("policy_decisions", []),
            approval_records=state.get("approval_records", []),
        )
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        error = f"Task Executor 调用失败: {type(exc).__name__}: {error_detail}"
        return {
            "plan": updated_plan,
            "step_outcome": StepExecutionOutcome(
                step_id=current_step_id,
                claimed_complete=False,
                action_count=0,
                error=error,
            ),
            "status": TaskStatus.FAILED,
            "error": error,
        }

    return _execution_state_update(
        state=state,
        updated_plan=updated_plan,
        execution=execution,
    )


def prepare_approval(state: TaskState) -> TaskStateUpdate:
    """在 interrupt 前提交 WAITING_APPROVAL，且不执行目标 Tool。"""

    pending = state.get("pending_action")
    if pending is None:
        return _failed_update("prepare_approval 需要 pending_action")
    if pending.step_id != state["current_step_id"]:
        return _failed_update("pending_action.step_id 与当前步骤不一致")
    return {"status": TaskStatus.WAITING_APPROVAL, "error": None}


def human_approval(state: TaskState) -> TaskStateUpdate:
    """通过 LangGraph interrupt 获取只含 approve/reject 的 resume value。"""

    pending = state.get("pending_action")
    if pending is None:
        return _failed_update("human_approval 需要 pending_action")
    decision = pending.policy_decision
    payload = {
        "type": "tool_approval",
        "call_id": pending.call_id,
        "step_id": pending.step_id,
        "tool_name": pending.tool_name,
        "risk_level": decision.risk_level.value,
        "reason": decision.reason,
        "arguments_preview": decision.preview,
        "tool_source": decision.tool_source,
    }
    # interrupt 之前没有外部副作用；resume 时该 node 会从头重放到这里。
    resume_value = interrupt(payload)
    try:
        response = ApprovalResponse.model_validate(resume_value)
    except ValidationError as exc:
        return {
            "status": TaskStatus.FAILED,
            "error": f"Human approval resume value 无效，Tool 未执行: {exc}",
        }
    record = ApprovalRecord(
        call_id=pending.call_id,
        step_id=pending.step_id,
        tool_name=pending.tool_name,
        decision=response.decision,
        reason=response.reason,
    )
    return {
        "approval_records": state.get("approval_records", []) + [record],
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def execute_approved_action(
    state: TaskState,
    *,
    executor: TaskExecutorLike,
) -> TaskStateUpdate:
    """不再询问模型，执行 PendingToolAction 中冻结的 exact action 一次。"""

    pending = state.get("pending_action")
    if pending is None:
        return _failed_update("execute_approved_action 需要 pending_action")
    result = await executor.execute_approved_action(
        pending_action=pending,
        approval_records=state.get("approval_records", []),
    )
    if result.fatal_error or result.tool_call is None:
        return {
            "status": TaskStatus.FAILED,
            "error": result.error or "获批 Tool action 未返回执行记录",
        }
    return {
        "tool_calls": state["tool_calls"] + [result.tool_call],
        "pending_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def handle_rejection(state: TaskState) -> TaskStateUpdate:
    """清除被拒动作，让 Executor 从审批历史中看到拒绝并选择替代方案。"""

    pending = state.get("pending_action")
    if pending is None:
        return _failed_update("handle_rejection 需要 pending_action")
    matches = [
        record
        for record in state.get("approval_records", [])
        if record.call_id == pending.call_id
        and record.step_id == pending.step_id
        and record.tool_name == pending.tool_name
    ]
    if len(matches) != 1 or matches[0].decision is not ApprovalChoice.REJECT:
        return _failed_update("被拒动作缺少唯一 REJECT approval record")
    return {
        "pending_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def verify_step(
    state: TaskState,
    *,
    verifier: TaskVerifierLike,
    max_step_attempts: int,
) -> TaskStateUpdate:
    """验证当前 Step 声明，并记录接受或拒绝结论。"""

    current_step_id = state["current_step_id"]
    task_spec = state["task_spec"]
    outcome = state["step_outcome"]
    if current_step_id is None or task_spec is None or outcome is None:
        return _failed_update("Step Verifier 无法运行: 当前步骤上下文不完整")

    step_index = _find_unique_step_index(state["plan"], current_step_id)
    if step_index is None:
        return _failed_update(
            f"Step Verifier 无法定位唯一当前步骤: {current_step_id}"
        )
    step = state["plan"][step_index]
    if outcome.step_id != step.id:
        return _failed_update(
            "StepExecutionOutcome.step_id 与当前步骤不一致: "
            f"expected={step.id}, actual={outcome.step_id}"
        )

    current_calls = [
        record for record in state["tool_calls"] if record.step_id == step.id
    ]
    dependency_results = [
        result
        for result in state["step_results"]
        if result.step_id in step.depends_on
    ]
    try:
        if outcome.claimed_complete:
            result = verifier.verify_step(
                task_spec=task_spec,
                step=step,
                outcome=outcome,
                tool_calls=current_calls,
                dependency_results=dependency_results,
            )
        else:
            # 尚未 claim complete 时确定性拒绝，不浪费一次模型调用。
            result = incomplete_outcome_rejection(step, outcome)
        validate_step_verification(step, result)
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        failed_step = step.model_copy(update={"status": PlanStepStatus.FAILED})
        return {
            "plan": _replace_step(state["plan"], step_index, failed_step),
            "status": TaskStatus.FAILED,
            "error": (
                f"Step Verifier 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }

    if result.verified:
        return {
            "step_verification": result,
            "status": TaskStatus.RUNNING,
            "error": None,
        }

    retry_count = step.retry_count + 1
    reached_limit = retry_count >= max_step_attempts
    rejected_step = step.model_copy(
        update={
            "retry_count": retry_count,
            "status": (
                PlanStepStatus.FAILED
                if reached_limit
                else PlanStepStatus.RUNNING
            ),
        }
    )
    update: TaskStateUpdate = {
        "plan": _replace_step(state["plan"], step_index, rejected_step),
        "step_verification": result,
        "status": TaskStatus.FAILED if reached_limit else TaskStatus.RUNNING,
        "error": None,
    }
    if reached_limit:
        update["error"] = (
            f"Step {step.id} 连续被 Verifier 拒绝，已达到 "
            f"max_step_attempts={max_step_attempts}: "
            f"{result.feedback or '未提供反馈'}"
        )
    return update


def advance_step(state: TaskState) -> TaskStateUpdate:
    """接受已验证 Claim，沉淀 StepResult 并选择下一可执行步骤。"""

    current_step_id = state["current_step_id"]
    outcome = state["step_outcome"]
    verification = state["step_verification"]
    if (
        current_step_id is None
        or outcome is None
        or verification is None
        or not verification.verified
    ):
        return _failed_update("advance_step 需要已通过验证的当前步骤")
    if outcome.step_id != current_step_id or verification.step_id != current_step_id:
        return _failed_update("advance_step 的步骤 ID 不一致")
    if not outcome.summary:
        return _failed_update("已验证的 StepExecutionOutcome 缺少 summary")

    step_index = _find_unique_step_index(state["plan"], current_step_id)
    if step_index is None:
        return _failed_update(
            f"advance_step 无法定位唯一当前步骤: {current_step_id}"
        )
    completed_step = state["plan"][step_index].model_copy(
        update={"status": PlanStepStatus.COMPLETED}
    )
    updated_plan = _replace_step(state["plan"], step_index, completed_step)
    result = StepResult(
        step_id=current_step_id,
        summary=outcome.summary,
        output=outcome.output,
        evidence=outcome.evidence,
    )
    updated_results = _upsert_step_result(
        state["step_results"],
        result,
        updated_plan,
    )

    try:
        next_step_id = find_next_executable_step(updated_plan)
    except ValueError as exc:
        return {
            "plan": updated_plan,
            "step_results": updated_results,
            "current_step_id": None,
            "step_outcome": None,
            "step_verification": None,
            "status": TaskStatus.FAILED,
            "error": str(exc),
        }

    if next_step_id is None and not all(
        step.status == PlanStepStatus.COMPLETED for step in updated_plan
    ):
        return {
            "plan": updated_plan,
            "step_results": updated_results,
            "current_step_id": None,
            "step_outcome": None,
            "step_verification": None,
            "status": TaskStatus.FAILED,
            "error": "Plan 状态异常: 没有 pending 步骤，但并非所有步骤已完成",
        }

    return {
        "plan": updated_plan,
        "step_results": updated_results,
        "current_step_id": next_step_id,
        # 新步骤不能继承上一条步骤的 Claim 或反馈。
        "step_outcome": None,
        "step_verification": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def verify_task(
    state: TaskState,
    *,
    verifier: TaskVerifierLike,
) -> TaskStateUpdate:
    """所有步骤完成后，独立验证 Task completion criteria。"""

    task_spec = state["task_spec"]
    if task_spec is None:
        return _failed_update("Task Verifier 无法运行: task_spec 缺失")
    try:
        result = verifier.verify_task(
            task_spec=task_spec,
            step_results=state["step_results"],
            task_results=state["task_results"],
        )
        validate_task_verification(result)
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        return {
            "status": TaskStatus.FAILED,
            "error": (
                f"Task Verifier 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }

    if result.completed:
        return {
            "verification": result,
            "current_step_id": None,
            "status": TaskStatus.COMPLETED,
            "error": None,
        }
    next_hint = f"; next_action={result.next_action}" if result.next_action else ""
    return {
        "verification": result,
        "current_step_id": None,
        "status": TaskStatus.FAILED,
        "error": f"Task Verifier 拒绝任务完成: {result.reason}{next_hint}",
    }


def find_next_executable_step(plan: Sequence[PlanStep]) -> int | None:
    """按计划顺序选择第一条依赖均已完成的 pending 步骤。"""

    completed_ids = {
        step.id for step in plan if step.status == PlanStepStatus.COMPLETED
    }
    pending_steps = [
        step for step in plan if step.status == PlanStepStatus.PENDING
    ]
    for step in pending_steps:
        if all(dependency_id in completed_ids for dependency_id in step.depends_on):
            return step.id
    if pending_steps:
        blocked = {
            step.id: [
                dependency_id
                for dependency_id in step.depends_on
                if dependency_id not in completed_ids
            ]
            for step in pending_steps
        }
        raise ValueError(
            "Plan 依赖阻塞: 存在 pending 步骤但没有可执行步骤; "
            f"unmet_dependencies={blocked}"
        )
    return None


def _execution_state_update(
    *,
    state: TaskState,
    updated_plan: list[PlanStep],
    execution: ExecutorRunResult,
) -> TaskStateUpdate:
    """把 Executor 返回值合并到 LangGraph 状态。"""

    update: TaskStateUpdate = {
        "plan": updated_plan,
        "tool_calls": state["tool_calls"] + execution.tool_calls,
        "policy_decisions": (
            state.get("policy_decisions", []) + execution.policy_decisions
        ),
        "step_outcome": execution.outcome,
        "pending_action": execution.pending_action,
        "status": (
            TaskStatus.FAILED if execution.fatal_error else TaskStatus.RUNNING
        ),
        "error": (
            execution.outcome.error
            if execution.fatal_error and execution.outcome is not None
            else None
        ),
    }
    if execution.fatal_error and execution.outcome is None:
        update["error"] = "Executor fatal failure 未提供 StepExecutionOutcome"
    if not execution.fatal_error and execution.outcome is None:
        if execution.pending_action is None:
            update["status"] = TaskStatus.FAILED
            update["error"] = "Executor 未返回 outcome 或 pending_action"
    return update


def _find_unique_step_index(plan: Sequence[PlanStep], step_id: int) -> int | None:
    matches = [index for index, step in enumerate(plan) if step.id == step_id]
    return matches[0] if len(matches) == 1 else None


def _replace_step(
    plan: Sequence[PlanStep],
    index: int,
    step: PlanStep,
) -> list[PlanStep]:
    updated = list(plan)
    updated[index] = step
    return updated


def _upsert_step_result(
    step_results: Sequence[StepResult],
    result: StepResult,
    plan: Sequence[PlanStep],
) -> list[StepResult]:
    """按 step_id 替换正式结果，并保持计划顺序且不产生重复。"""

    by_step_id = {
        item.step_id: item for item in step_results if item.step_id != result.step_id
    }
    by_step_id[result.step_id] = result
    plan_order = {step.id: index for index, step in enumerate(plan)}
    return sorted(
        by_step_id.values(),
        key=lambda item: plan_order.get(item.step_id, len(plan)),
    )


def _failed_update(error: str) -> TaskStateUpdate:
    return {"status": TaskStatus.FAILED, "error": error}


def _route_after_plan(state: TaskState) -> Literal["execute_step", "end"]:
    return "end" if state["status"] == TaskStatus.FAILED else "execute_step"


def _route_after_execute(
    state: TaskState,
) -> Literal["prepare_approval", "verify_step", "end"]:
    if state["status"] == TaskStatus.FAILED:
        return "end"
    if state.get("pending_action") is not None:
        return "prepare_approval"
    return "verify_step"


def _route_after_human_approval(
    state: TaskState,
) -> Literal["execute_approved_action", "handle_rejection", "end"]:
    if state["status"] == TaskStatus.FAILED:
        return "end"
    records = state.get("approval_records", [])
    if not records:
        return "end"
    return (
        "execute_approved_action"
        if records[-1].decision is ApprovalChoice.APPROVE
        else "handle_rejection"
    )


def _route_after_step_verification(
    state: TaskState,
) -> Literal["advance_step", "execute_step", "end"]:
    if state["status"] == TaskStatus.FAILED:
        return "end"
    verification = state["step_verification"]
    if verification is not None and verification.verified:
        return "advance_step"
    return "execute_step"


def _route_after_advance(
    state: TaskState,
) -> Literal["execute_step", "verify_task", "end"]:
    if state["status"] == TaskStatus.FAILED:
        return "end"
    if state["current_step_id"] is None:
        return "verify_task"
    return "execute_step"


def build_graph(
    analyzer: TaskAnalyzerLike | None = None,
    planner: TaskPlannerLike | None = None,
    executor: TaskExecutorLike | None = None,
    verifier: TaskVerifierLike | None = None,
    max_step_attempts: int = 3,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph:
    """构建带 Policy/HITL、两层验证和有限重试的状态图。"""

    if max_step_attempts < 1:
        raise ValueError("max_step_attempts 必须至少为 1")
    active_analyzer = analyzer if analyzer is not None else TaskAnalyzer()
    active_planner = planner if planner is not None else TaskPlanner()
    active_executor = (
        executor
        if executor is not None
        else TaskExecutor(registry=ToolRegistry())
    )
    active_verifier = verifier if verifier is not None else TaskVerifier()

    builder = StateGraph(TaskState)
    builder.add_node("initialize_task", initialize_task)
    builder.add_node(
        "analyze_task",
        partial(analyze_task, analyzer=active_analyzer),
    )
    builder.add_node("plan_task", partial(plan_task, planner=active_planner))
    builder.add_node(
        "execute_step",
        partial(execute_step, executor=active_executor),
    )
    builder.add_node("prepare_approval", prepare_approval)
    builder.add_node("human_approval", human_approval)
    builder.add_node(
        "execute_approved_action",
        partial(execute_approved_action, executor=active_executor),
    )
    builder.add_node("handle_rejection", handle_rejection)
    builder.add_node(
        "verify_step",
        partial(
            verify_step,
            verifier=active_verifier,
            max_step_attempts=max_step_attempts,
        ),
    )
    builder.add_node("advance_step", advance_step)
    builder.add_node(
        "verify_task",
        partial(verify_task, verifier=active_verifier),
    )

    builder.add_edge(START, "initialize_task")
    builder.add_edge("initialize_task", "analyze_task")
    builder.add_edge("analyze_task", "plan_task")
    builder.add_conditional_edges(
        "plan_task",
        _route_after_plan,
        {"execute_step": "execute_step", "end": END},
    )
    builder.add_conditional_edges(
        "execute_step",
        _route_after_execute,
        {
            "prepare_approval": "prepare_approval",
            "verify_step": "verify_step",
            "end": END,
        },
    )
    builder.add_edge("prepare_approval", "human_approval")
    builder.add_conditional_edges(
        "human_approval",
        _route_after_human_approval,
        {
            "execute_approved_action": "execute_approved_action",
            "handle_rejection": "handle_rejection",
            "end": END,
        },
    )
    builder.add_edge("execute_approved_action", "execute_step")
    builder.add_edge("handle_rejection", "execute_step")
    builder.add_conditional_edges(
        "verify_step",
        _route_after_step_verification,
        {
            "advance_step": "advance_step",
            "execute_step": "execute_step",
            "end": END,
        },
    )
    builder.add_conditional_edges(
        "advance_step",
        _route_after_advance,
        {
            "execute_step": "execute_step",
            "verify_task": "verify_task",
            "end": END,
        },
    )
    builder.add_edge("verify_task", END)
    return builder.compile(checkpointer=checkpointer)
```

## 21. prepare_approval

只验证 pending identity、把 status 更新为 WAITING_APPROVAL，不执行 Tool、不调用 interrupt、不修改 arguments。该 node 先完成并 checkpoint，下一 superstep 才进入 human node。

## 22. human_approval Interrupt Node

payload 包含 type/call_id/step_id/tool_name/risk/reason/redacted preview/source。Node 在 `interrupt(payload)` 前没有外部副作用，也没有错误地捕获 interrupt。resume 返回后才用 ApprovalResponse validation；非法 value 进入 FAILED 且 Tool 未执行。

## 23. Exact Approved Execution

Approve 后 Graph 直接传 `state.pending_action` 给 `TaskExecutor.execute_approved_action`。它要求唯一匹配的 APPROVE record，然后执行冻结的 exact name + exact arguments 一次。Graph 不访问 Executor 私有 Registry，LLM 不重新生成参数，Policy 不重复判断。

## 24. Rejection Feedback Path

Reject 创建 ApprovalRecord，清除 pending，status 恢复 RUNNING，然后回到 execute_step。Executor context 只加入当前 Step 的 policy/approval history；记录包含被拒 call/tool/reason，不含原始 secret arguments。Reject 不创建 ToolCallRecord、不调用目标 Tool，也不自动使 Task failed。

## 25. In-memory Checkpointer

Phase 8 使用 `MemorySaver` 和 `thread_id=task_id`。它只保证当前进程生命周期内的 interrupt/resume；进程重启后的恢复、durable SQLite checkpoint、crash exactly-once 和 idempotency ledger 都属于 Phase 9。

## 26. CLI Approval Loop

Host 使用同一 AsyncExitStack 管理 MCP/Browser，启用任一外部 Provider 时构造 DefaultRiskPolicy。CLI 从 public StateSnapshot tasks 读取 interrupt，打印 tool/risk/reason/redacted preview，使用 `await asyncio.to_thread(input, ...)` 接收 y/N，并以同一 thread_id 传 Command resume。没有 `--approve-all`、`--unsafe` 或 `--skip-confirmation`。

### `src/taskpilot/cli.py`

```python
"""TaskPilot 的 async Host/CLI 入口。"""

import argparse
import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from taskpilot.browser import BrowserConfig, BrowserToolProvider
from taskpilot.executor import TaskExecutor
from taskpilot.graph import build_graph
from taskpilot.mcp.config import load_mcp_server_configs
from taskpilot.mcp.provider import MCPToolProvider
from taskpilot.models import TaskStatus
from taskpilot.policy import DefaultRiskPolicy
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry


def create_initial_state(user_input: str) -> TaskState:
    """根据用户输入创建结构化初始任务状态。"""

    return {
        "task_id": str(uuid4()),
        "user_input": user_input,
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


async def async_main(argv: Sequence[str] | None = None) -> int:
    """在可选 MCP/Browser 生命周期内异步运行一次 TaskPilot Graph。"""

    parser = argparse.ArgumentParser(description="Run the TaskPilot graph.")
    parser.add_argument("task", help="Task description")
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
    args = parser.parse_args(argv)

    if args.headed and not args.browser:
        parser.error("--headed requires --browser")

    registry = ToolRegistry()
    initial_state = create_initial_state(args.task)
    # Host 持有所有外部资源；Graph 只接收已配置好的 Executor。
    async with AsyncExitStack() as stack:
        if args.mcp_config is not None:
            configs = load_mcp_server_configs(args.mcp_config)
            provider = await stack.enter_async_context(MCPToolProvider(configs))
            await provider.register_tools(registry)
        if args.browser:
            browser_provider = await stack.enter_async_context(
                BrowserToolProvider(BrowserConfig(headless=not args.headed))
            )
            browser_provider.register_tools(registry)

        # 真实外部 Tool 默认经过 Policy；没有危险的 auto-approve 开关。
        policy = DefaultRiskPolicy() if (args.browser or args.mcp_config) else None
        executor = TaskExecutor(registry=registry, policy=policy)
        graph = build_graph(executor=executor, checkpointer=MemorySaver())
        graph_config = {
            "configurable": {"thread_id": initial_state["task_id"]}
        }
        result = await graph.ainvoke(initial_state, config=graph_config)
        snapshot = graph.get_state(graph_config)
        interrupts = [item for task in snapshot.tasks for item in task.interrupts]
        while interrupts:
            approval = interrupts[0].value
            print(f"approval_tool={approval['tool_name']}")
            print(f"approval_risk={approval['risk_level']}")
            print(f"approval_reason={approval['reason']}")
            print(
                "approval_arguments_preview="
                + json.dumps(
                    approval["arguments_preview"],
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            answer = await asyncio.to_thread(input, "Approve? [y/N] ")
            resume = (
                {"decision": "approve"}
                if answer.strip().lower() == "y"
                else {"decision": "reject", "reason": "Rejected from CLI"}
            )
            result = await graph.ainvoke(
                Command(resume=resume),
                config=graph_config,
            )
            snapshot = graph.get_state(graph_config)
            interrupts = [
                item
                for task in snapshot.tasks
                for item in task.interrupts
            ]
    status = result["status"]
    status_value = status.value if isinstance(status, TaskStatus) else str(status)
    print(f"task_id={result['task_id']}")
    print(f"status={status_value}")
    task_spec = result["task_spec"]
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
    for step in result["plan"]:
        print(f"  step_id={step.id}")
        print(f"    description={step.description}")
        print(f"    depends_on={json.dumps(step.depends_on)}")
        print(
            "    success_criteria="
            + json.dumps(step.success_criteria, ensure_ascii=False)
        )
        print(f"    status={step.status.value}")
    print(f"current_step_id={result['current_step_id']}")
    step_outcome = result.get("step_outcome")
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
            [item.model_dump(mode="json") for item in result["step_results"]],
            ensure_ascii=False,
        )
    )
    verification = result.get("verification")
    if verification is not None:
        print(f"task_verified={verification.completed}")
        print(f"verification_reason={verification.reason}")
    if result.get("error"):
        print(f"error={result['error']}")
        return 1
    if step_outcome is not None and step_outcome.error:
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 顶层创建一次事件循环；Graph node 内不会创建 event loop。"""

    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
```

## 27. Full Relevant Tests

以下为当前最终源码，不是 diff。

### `tests/fixtures/browser_site/index.html`

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>TaskPilot Browser Fixture</title>
  </head>
  <body>
    <main>
      <h1>TaskPilot Browser Fixture</h1>

      <label for="query">Search Query</label>
      <input id="query" name="query" placeholder="Enter query">

      <label for="category">Category</label>
      <select id="category" name="category">
        <option value="gyms">Gyms</option>
        <option value="cafes">Cafes</option>
      </select>

      <label>
        <input id="inactive" type="checkbox">
        Include inactive
      </label>
      <button id="apply" type="button">Apply filters</button>

      <section id="results" aria-live="polite">No results yet.</section>
      <p id="css-fallback">CSS fallback target</p>

      <form id="profile-form">
        <label for="profile-name">Profile name</label>
        <input id="profile-name" name="profile-name">
        <button type="submit">Submit profile</button>
      </form>
      <p id="submit-status">Profile not submitted.</p>

      <button id="delete-item" type="button">Delete item</button>
      <p id="delete-status">Item exists.</p>

      <section id="delayed-controls" aria-label="Delayed controls"></section>
      <p id="delayed-status">Delayed action waiting.</p>

      <a href="/details.html">Open details</a>
      <a href="/details.html" target="_blank">Open details popup</a>
      <a href="/download.txt" download="report.txt">Download report</a>

      <iframe title="Demo frame" src="/frame.html"></iframe>

      <button type="button">Duplicate action</button>
      <button type="button">Duplicate action</button>
    </main>
    <script>
      document.querySelector("#apply").addEventListener("click", () => {
        const query = document.querySelector("#query").value;
        const category = document.querySelector("#category").value;
        const inactive = document.querySelector("#inactive").checked;
        document.querySelector("#results").textContent =
          `Results for ${query}; category=${category}; inactive=${inactive}: Harbor Gym, Peak Fitness`;
      });
      document.querySelector("#profile-form").addEventListener("submit", (event) => {
        event.preventDefault();
        document.querySelector("#submit-status").textContent = "Profile submitted.";
      });
      document.querySelector("#delete-item").addEventListener("click", () => {
        document.querySelector("#delete-status").textContent = "Item deleted.";
      });
      window.setTimeout(() => {
        const controls = document.querySelector("#delayed-controls");
        controls.innerHTML = `
          <label for="delayed-input">Delayed Input</label>
          <input id="delayed-input">
          <button id="delayed-apply" type="button">Delayed Apply</button>
        `;
        document.querySelector("#delayed-apply").addEventListener("click", () => {
          const value = document.querySelector("#delayed-input").value;
          document.querySelector("#delayed-status").textContent = `Delayed value: ${value}`;
        });
      }, 250);
    </script>
  </body>
</html>
```

### `tests/fixtures/browser_site/delayed.html`

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Delayed DOM Fixture</title>
  </head>
  <body>
    <main>
      <h1>Delayed DOM Fixture</h1>
      <section id="mount"></section>
      <p id="delayed-result">Waiting for delayed action.</p>
    </main>
    <script>
      window.setTimeout(() => {
        document.querySelector("#mount").innerHTML = `
          <label for="late-input">Late Input</label>
          <input id="late-input">
          <button type="button" id="late-apply">Late Apply</button>
        `;
        document.querySelector("#late-apply").addEventListener("click", () => {
          const value = document.querySelector("#late-input").value;
          document.querySelector("#delayed-result").textContent = `Late value: ${value}`;
        });
      }, 500);
    </script>
  </body>
</html>
```

### `tests/fixtures/mcp_server.py`

```python
"""Phase 6 离线集成测试使用的最小官方 MCP v2 STDIO Server。"""

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations


server = MCPServer("taskpilot-phase-6-demo")


@server.tool(
    description="Echo text through the real local MCP subprocess.",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
    structured_output=True,
)
def echo(text: str) -> dict[str, str]:
    """返回输入文本，证明请求确实到达独立 MCP Server。"""

    return {"echoed": text}


@server.tool(
    description="Add two integers in the real local MCP subprocess.",
    structured_output=True,
)
def add(a: int, b: int) -> dict[str, int]:
    """返回两个整数的和。"""

    return {"sum": a + b}


@server.tool(
    description="Return a deterministic destructive-action fixture result.",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=False,
    ),
    structured_output=True,
)
def destructive_dummy(target: str) -> dict[str, str]:
    """不删除文件，只用于验证 MCP destructive annotation 的风险分类。"""

    return {"would_delete": target}


@server.tool(description="Always fail to exercise MCP is_error handling.")
def fail_tool() -> str:
    """故意抛错；官方 Server 会转换为 is_error=true。"""

    raise ToolError("intentional MCP failure")


if __name__ == "__main__":
    server.run(transport="stdio")
```

### `tests/test_policy.py`

```python
"""Phase 8 deterministic Risk Policy 的离线单元测试。"""

from dataclasses import dataclass, field
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import ValidationError

from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.models import PlanStep, TaskSpec
from taskpilot.policy import (
    ApprovalResponse,
    DefaultRiskPolicy,
    PolicyOutcome,
    RiskLevel,
    redact_preview,
)
from taskpilot.tools import ToolRegistry


@dataclass
class MetadataTool:
    name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    description: str = "Policy fixture tool"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object"}
    )

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return arguments


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("risk", "expected_level", "expected_outcome"),
    [
        ("low", RiskLevel.LOW, PolicyOutcome.ALLOW),
        ("medium", RiskLevel.MEDIUM, PolicyOutcome.ALLOW),
        ("high", RiskLevel.HIGH, PolicyOutcome.REQUIRE_APPROVAL),
    ],
)
async def test_explicit_local_risk_mapping(
    risk: str,
    expected_level: RiskLevel,
    expected_outcome: PolicyOutcome,
) -> None:
    decision = await DefaultRiskPolicy().assess(
        call_id=f"local-{risk}",
        step_id=1,
        tool=MetadataTool(
            name="local_fixture",
            metadata={"source": "local", "risk": risk},
        ),
        arguments={"value": "safe"},
    )

    assert decision.risk_level is expected_level
    assert decision.outcome is expected_outcome
    assert decision.tool_source == "local"


@pytest.mark.asyncio
async def test_unknown_local_tool_defaults_to_high() -> None:
    decision = await DefaultRiskPolicy().assess(
        call_id="unknown-1",
        step_id=1,
        tool=MetadataTool(name="unknown", metadata={}),
        arguments={},
    )

    assert decision.risk_level is RiskLevel.HIGH
    assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL


@pytest.mark.asyncio
async def test_mcp_annotation_trust_boundary() -> None:
    annotations = {
        "read_only_hint": True,
        "destructive_hint": False,
        "open_world_hint": False,
    }
    trusted = MetadataTool(
        name="trusted__read",
        metadata={
            "source": "mcp",
            "trust_tool_annotations": True,
            "annotations": annotations,
        },
    )
    untrusted = MetadataTool(
        name="untrusted__read",
        metadata={
            "source": "mcp",
            "trust_tool_annotations": False,
            "annotations": annotations,
        },
    )

    trusted_decision = await DefaultRiskPolicy().assess(
        call_id="mcp-trusted",
        step_id=1,
        tool=trusted,
        arguments={},
    )
    untrusted_decision = await DefaultRiskPolicy().assess(
        call_id="mcp-untrusted",
        step_id=1,
        tool=untrusted,
        arguments={},
    )

    assert trusted_decision.risk_level is RiskLevel.LOW
    assert trusted_decision.annotations_trusted is True
    assert untrusted_decision.risk_level is RiskLevel.HIGH
    assert untrusted_decision.annotations_trusted is False


@pytest.mark.asyncio
async def test_trusted_destructive_mcp_annotation_is_high() -> None:
    tool = MetadataTool(
        name="trusted__delete",
        metadata={
            "source": "mcp",
            "trust_tool_annotations": True,
            "annotations": {
                "read_only_hint": False,
                "destructive_hint": True,
                "open_world_hint": False,
            },
        },
    )

    decision = await DefaultRiskPolicy().assess(
        call_id="mcp-delete",
        step_id=1,
        tool=tool,
        arguments={"target": "fixture"},
    )

    assert decision.risk_level is RiskLevel.HIGH
    assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL


def test_sensitive_preview_is_recursive_and_does_not_mutate_arguments() -> None:
    arguments = {
        "recipient": "alice",
        "api_key": "super-secret",
        "payload": {"password": "private", "message": "hello"},
        "items": [{"authorization": "Bearer hidden", "value": 3}],
    }

    preview = redact_preview(arguments)

    assert preview == {
        "recipient": "alice",
        "api_key": "[REDACTED]",
        "payload": {"password": "[REDACTED]", "message": "hello"},
        "items": [{"authorization": "[REDACTED]", "value": 3}],
    }
    assert arguments["api_key"] == "super-secret"
    assert arguments["payload"]["password"] == "private"


def test_approval_response_forbids_argument_editing() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ApprovalResponse.model_validate(
            {
                "decision": "approve",
                "arguments": {"recipient": "mallory"},
            }
        )


class DenySequenceModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = responses
        self.invocations: list[list[BaseMessage]] = []

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> "DenySequenceModel":
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations.append(list(messages))
        return self.responses.pop(0)


def _model_call(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": arguments,
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


@pytest.mark.asyncio
async def test_explicit_policy_deny_is_feedback_and_never_invokes_tool() -> None:
    tool = MetadataTool(
        name="blocked_local",
        metadata={
            "source": "local",
            "risk": "high",
            "policy_outcome": "deny",
            "policy_reason": "Host block fixture",
        },
    )
    model = DenySequenceModel(
        [
            _model_call("blocked_local", {}, "deny-1"),
            _model_call(
                FINISH_STEP_NAME,
                {
                    "summary": "The blocked action was avoided.",
                    "evidence": ["Policy denial was observed."],
                    "output": {"blocked": True},
                },
                "finish-2",
            ),
        ]
    )
    registry = ToolRegistry()
    registry.register(tool)
    executor = TaskExecutor(
        registry=registry,
        model=cast(BaseChatModel, model),
        policy=DefaultRiskPolicy(),
    )

    result = await executor.execute(
        task_spec=TaskSpec(goal="Avoid a blocked action"),
        step=PlanStep(id=1, description="Use an allowed path"),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome is not None
    assert result.outcome.claimed_complete is True
    assert result.tool_calls == []
    assert result.policy_decisions[0].outcome is PolicyOutcome.DENY
    denial_message = model.invocations[1][-1]
    assert isinstance(denial_message, ToolMessage)
    assert denial_message.tool_call_id == "deny-1"
    assert denial_message.status == "error"
```

### `tests/test_hitl.py`

```python
"""真实 LangGraph interrupt/Command resume 的 Phase 8 HITL 集成测试。"""

import json
from dataclasses import dataclass, field
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.graph import build_graph
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    VerificationResult,
)
from taskpilot.policy import DefaultRiskPolicy
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry


class FakeFunctionCallingModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.invocations: list[list[BaseMessage]] = []

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> "FakeFunctionCallingModel":
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations.append(list(messages))
        return self.responses.pop(0)


def tool_call(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": arguments,
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def finish_call(call_id: str = "finish") -> AIMessage:
    return tool_call(
        FINISH_STEP_NAME,
        {
            "summary": "The safe path produced enough deterministic evidence.",
            "evidence": ["The expected local Tool result was observed."],
            "output": {"completed": True},
        },
        call_id,
    )


@dataclass
class CountingTool:
    name: str
    risk: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    description: str = "Deterministic local HITL fixture"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "recipient": {"type": "string"},
                "message": {"type": "string"},
                "api_key": {"type": "string"},
                "payload": {"type": "object"},
            },
            "additionalProperties": False,
        }
    )

    @property
    def metadata(self) -> dict[str, str]:
        return {"source": "local", "risk": self.risk}

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        return {"call_count": len(self.calls), "received": dict(arguments)}


@dataclass
class FailingCountingTool(CountingTool):
    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        raise RuntimeError("controlled approved action failure")


class FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(
            goal=user_input,
            completion_criteria=["Finish one deterministic local step"],
        )


class FakePlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="Use the approved or safe local action",
                    success_criteria=["A safe result is available"],
                )
            ]
        )


class FakeVerifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        return StepVerificationResult(
            step_id=kwargs["step"].id,
            verified=True,
            checks=[
                CriterionCheck(
                    criterion_index=0,
                    satisfied=True,
                    reason="Deterministic fixture accepted",
                )
            ],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(completed=True, reason="Fixture task completed")


def initial_state(task_id: str) -> TaskState:
    return {
        "task_id": task_id,
        "user_input": "Run a deterministic HITL fixture",
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


def make_graph(
    tools: Sequence[CountingTool],
    model: FakeFunctionCallingModel,
) -> CompiledStateGraph:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    executor = TaskExecutor(
        registry=registry,
        model=cast(BaseChatModel, model),
        policy=DefaultRiskPolicy(),
    )
    return build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor,
        verifier=FakeVerifier(),
        checkpointer=MemorySaver(),
    )


def interrupt_values(
    graph: CompiledStateGraph,
    config: dict[str, Any],
) -> list[Any]:
    snapshot = graph.get_state(config)
    return [item for task in snapshot.tasks for item in task.interrupts]


@pytest.mark.asyncio
async def test_real_interrupt_approve_executes_exact_action_once() -> None:
    tool = CountingTool(name="send_message", risk="high")
    exact_arguments = {"recipient": "alice", "message": "hello"}
    model = FakeFunctionCallingModel(
        [
            tool_call("send_message", exact_arguments, "send-1"),
            finish_call("finish-2"),
        ]
    )
    graph = make_graph([tool], model)
    config = {"configurable": {"thread_id": "hitl-approve"}}

    paused = await graph.ainvoke(initial_state("hitl-approve"), config=config)

    assert tool.calls == []
    assert paused["status"] is TaskStatus.WAITING_APPROVAL
    assert paused["pending_action"].arguments == exact_arguments
    interrupts = interrupt_values(graph, config)
    assert len(interrupts) == 1
    assert interrupts[0].value["tool_name"] == "send_message"
    assert interrupts[0].value["arguments_preview"] == exact_arguments

    final = await graph.ainvoke(
        Command(resume={"decision": "approve"}),
        config=config,
    )

    assert tool.calls == [exact_arguments]
    assert final["status"] is TaskStatus.COMPLETED
    assert final.get("pending_action") is None
    assert len(final["tool_calls"]) == 1
    assert final["tool_calls"][0].call_id == "send-1"
    assert final["approval_records"][0].decision.value == "approve"
    print(
        "HITL_APPROVE_SMOKE="
        + json.dumps(
            {
                "paused_status": paused["status"].value,
                "interrupt": interrupts[0].value,
                "counter_before": 0,
                "counter_after": len(tool.calls),
                "actual_arguments": tool.calls[0],
                "final_status": final["status"].value,
            },
            ensure_ascii=True,
        )
    )


@pytest.mark.asyncio
async def test_reject_never_executes_high_risk_and_can_use_low_risk_alternative() -> None:
    high = CountingTool(name="local_write", risk="high")
    low = CountingTool(name="local_read", risk="low")
    model = FakeFunctionCallingModel(
        [
            tool_call("local_write", {"message": "blocked"}, "write-1"),
            tool_call("local_read", {"message": "safe"}, "read-2"),
            finish_call("finish-3"),
        ]
    )
    graph = make_graph([high, low], model)
    config = {"configurable": {"thread_id": "hitl-reject"}}

    paused = await graph.ainvoke(initial_state("hitl-reject"), config=config)
    final = await graph.ainvoke(
        Command(resume={"decision": "reject", "reason": "Use read-only path"}),
        config=config,
    )

    assert paused["status"] is TaskStatus.WAITING_APPROVAL
    assert high.calls == []
    assert low.calls == [{"message": "safe"}]
    assert final["status"] is TaskStatus.COMPLETED
    assert [record.tool_name for record in final["tool_calls"]] == ["local_read"]
    assert final["approval_records"][0].decision.value == "reject"
    second_context = json.loads(
        cast(HumanMessage, model.invocations[1][1]).content
    )
    assert second_context["approval_history"][0]["reason"] == "Use read-only path"
    print(
        "HITL_REJECT_ALTERNATIVE_SMOKE="
        + json.dumps(
            {
                "high_risk_calls": len(high.calls),
                "low_risk_calls": len(low.calls),
                "approval": final["approval_records"][0].model_dump(mode="json"),
                "final_status": final["status"].value,
            },
            ensure_ascii=True,
        )
    )


@pytest.mark.asyncio
async def test_sensitive_preview_redacted_but_approved_tool_receives_originals() -> None:
    tool = CountingTool(name="secure_send", risk="high")
    arguments = {
        "recipient": "alice",
        "api_key": "super-secret",
        "payload": {"password": "private", "message": "hello"},
    }
    model = FakeFunctionCallingModel(
        [tool_call("secure_send", arguments, "secure-1"), finish_call("finish-2")]
    )
    graph = make_graph([tool], model)
    config = {"configurable": {"thread_id": "hitl-secret"}}

    paused = await graph.ainvoke(initial_state("hitl-secret"), config=config)
    preview = interrupt_values(graph, config)[0].value["arguments_preview"]
    assert preview == {
        "recipient": "alice",
        "api_key": "[REDACTED]",
        "payload": {"password": "[REDACTED]", "message": "hello"},
    }
    assert "super-secret" not in json.dumps(preview)
    assert "private" not in json.dumps(preview)

    final = await graph.ainvoke(
        Command(resume={"decision": "approve"}),
        config=config,
    )

    assert tool.calls == [arguments]
    assert final["status"] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_invalid_resume_cannot_edit_or_execute_pending_action() -> None:
    tool = CountingTool(name="local_write", risk="high")
    model = FakeFunctionCallingModel(
        [tool_call("local_write", {"message": "original"}, "edit-1")]
    )
    graph = make_graph([tool], model)
    config = {"configurable": {"thread_id": "hitl-invalid"}}

    await graph.ainvoke(initial_state("hitl-invalid"), config=config)
    result = await graph.ainvoke(
        Command(
            resume={
                "decision": "approve",
                "arguments": {"message": "modified"},
            }
        ),
        config=config,
    )

    assert tool.calls == []
    assert result["status"] is TaskStatus.FAILED
    assert "resume value 无效" in result["error"]


@pytest.mark.asyncio
async def test_approved_tool_environment_error_is_recorded_and_can_recover() -> None:
    failing = FailingCountingTool(name="high_failing_write", risk="high")
    low = CountingTool(name="local_read", risk="low")
    model = FakeFunctionCallingModel(
        [
            tool_call(
                "high_failing_write",
                {"message": "attempt once"},
                "failing-1",
            ),
            tool_call("local_read", {"message": "fallback"}, "fallback-2"),
            finish_call("finish-3"),
        ]
    )
    graph = make_graph([failing, low], model)
    config = {"configurable": {"thread_id": "hitl-approved-failure"}}

    await graph.ainvoke(initial_state("hitl-approved-failure"), config=config)
    final = await graph.ainvoke(
        Command(resume={"decision": "approve"}),
        config=config,
    )

    assert failing.calls == [{"message": "attempt once"}]
    assert low.calls == [{"message": "fallback"}]
    assert [record.status.value for record in final["tool_calls"]] == [
        "failed",
        "success",
    ]
    assert final["status"] is TaskStatus.COMPLETED
```

### `tests/test_browser_policy.py`

```python
"""真实 Chromium Browser 风险 preflight 与 Graph HITL 测试。"""

import json
from pathlib import Path
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from taskpilot.browser import BrowserConfig, BrowserLocatorError, BrowserToolProvider
from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.graph import build_graph
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    VerificationResult,
)
from taskpilot.policy import DefaultRiskPolicy, PolicyOutcome, RiskLevel
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry


class FakeFunctionCallingModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.invocations: list[list[BaseMessage]] = []

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> "FakeFunctionCallingModel":
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations.append(list(messages))
        return self.responses.pop(0)


def call(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": arguments,
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def finish(call_id: str) -> AIMessage:
    return call(
        FINISH_STEP_NAME,
        {
            "summary": "The localhost browser path was observed.",
            "evidence": ["Rendered DOM status was extracted."],
            "output": {"browser_fixture": True},
        },
        call_id,
    )


class FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(
            goal=user_input,
            completion_criteria=["Observe the requested localhost DOM state"],
        )


class FakePlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="Interact with the deterministic localhost fixture",
                    success_criteria=["The expected DOM state is observed"],
                )
            ]
        )


class FakeVerifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        return StepVerificationResult(
            step_id=kwargs["step"].id,
            verified=True,
            checks=[
                CriterionCheck(
                    criterion_index=0,
                    satisfied=True,
                    reason="Local deterministic Browser fixture accepted",
                )
            ],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(completed=True, reason="Browser fixture completed")


def browser_config(test_name: str) -> BrowserConfig:
    root = Path("tests/.artifacts/browser_policy").resolve() / test_name
    return BrowserConfig(
        headless=True,
        action_timeout_ms=3_000,
        navigation_timeout_ms=5_000,
        download_dir=root / "downloads",
        artifact_dir=root / "screenshots",
    )


def initial_state(task_id: str) -> TaskState:
    return {
        "task_id": task_id,
        "user_input": "Run a localhost Browser policy fixture",
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


def make_graph(
    registry: ToolRegistry,
    model: FakeFunctionCallingModel,
    *,
    max_actions: int = 6,
) -> CompiledStateGraph:
    executor = TaskExecutor(
        registry=registry,
        model=cast(BaseChatModel, model),
        max_actions_per_step=max_actions,
        policy=DefaultRiskPolicy(),
    )
    return build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor,
        verifier=FakeVerifier(),
        checkpointer=MemorySaver(),
    )


def interrupt_values(
    graph: CompiledStateGraph,
    config: dict[str, Any],
) -> list[Any]:
    snapshot = graph.get_state(config)
    return [item for task in snapshot.tasks for item in task.interrupts]


def locator(role: str, name: str) -> dict[str, Any]:
    return {"strategy": "role", "role": role, "name": name}


@pytest.mark.asyncio
async def test_browser_click_preflight_risk_rules_without_side_effect(
    browser_site_url: str,
) -> None:
    registry = ToolRegistry()
    policy = DefaultRiskPolicy()
    async with BrowserToolProvider(browser_config("preflight")) as provider:
        provider.register_tools(registry)
        await registry.invoke("browser_navigate", {"url": browser_site_url})
        click_tool = registry.get("browser_click")
        navigate = await policy.assess(
            call_id="risk-nav",
            step_id=1,
            tool=registry.get("browser_navigate"),
            arguments={"url": browser_site_url},
        )
        fill = await policy.assess(
            call_id="risk-fill",
            step_id=1,
            tool=registry.get("browser_fill"),
            arguments={
                "locator": {"strategy": "label", "value": "Search Query"},
                "value": "gyms",
            },
        )
        apply = await policy.assess(
            call_id="risk-apply",
            step_id=1,
            tool=click_tool,
            arguments={"locator": locator("button", "Apply filters")},
        )
        submit = await policy.assess(
            call_id="risk-submit",
            step_id=1,
            tool=click_tool,
            arguments={"locator": locator("button", "Submit profile")},
        )
        delete = await policy.assess(
            call_id="risk-delete",
            step_id=1,
            tool=click_tool,
            arguments={"locator": locator("button", "Delete item")},
        )
        link = await policy.assess(
            call_id="risk-link",
            step_id=1,
            tool=click_tool,
            arguments={
                "locator": {
                    **locator("link", "Open details"),
                    "exact": True,
                }
            },
        )
        with pytest.raises(BrowserLocatorError, match="strict mode violation"):
            await policy.assess(
                call_id="risk-ambiguous",
                step_id=1,
                tool=click_tool,
                arguments={"locator": locator("button", "Duplicate action")},
            )
        submit_status = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#submit-status"}},
        )
        delete_status = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#delete-status"}},
        )

    assert navigate.risk_level is RiskLevel.LOW
    assert fill.risk_level is RiskLevel.MEDIUM
    assert apply.risk_level is RiskLevel.MEDIUM
    assert submit.risk_level is RiskLevel.HIGH
    assert submit.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert delete.risk_level is RiskLevel.HIGH
    assert link.risk_level is RiskLevel.LOW
    assert submit_status["text"] == "Profile not submitted."
    assert delete_status["text"] == "Item exists."


@pytest.mark.asyncio
async def test_low_and_medium_browser_actions_do_not_interrupt(
    browser_site_url: str,
) -> None:
    model = FakeFunctionCallingModel(
        [
            call("browser_navigate", {"url": browser_site_url}, "normal-nav"),
            call("browser_observe", {}, "normal-observe"),
            call(
                "browser_fill",
                {
                    "locator": {"strategy": "label", "value": "Search Query"},
                    "value": "gyms",
                },
                "normal-fill",
            ),
            call(
                "browser_click",
                {"locator": locator("button", "Apply filters")},
                "normal-click",
            ),
            call(
                "browser_extract_text",
                {"locator": {"strategy": "css", "value": "#results"}},
                "normal-extract",
            ),
            finish("normal-finish"),
        ]
    )
    registry = ToolRegistry()
    async with BrowserToolProvider(browser_config("normal")) as provider:
        provider.register_tools(registry)
        graph = make_graph(registry, model)
        config = {"configurable": {"thread_id": "browser-normal"}}
        result = await graph.ainvoke(initial_state("browser-normal"), config=config)

    assert interrupt_values(graph, config) == []
    assert result["status"] is TaskStatus.COMPLETED
    assert [item.risk_level for item in result["policy_decisions"]] == [
        RiskLevel.LOW,
        RiskLevel.LOW,
        RiskLevel.MEDIUM,
        RiskLevel.MEDIUM,
        RiskLevel.LOW,
    ]


@pytest.mark.asyncio
async def test_browser_submit_waits_for_approval_then_executes(
    browser_site_url: str,
) -> None:
    model = FakeFunctionCallingModel(
        [
            call("browser_navigate", {"url": browser_site_url}, "submit-nav"),
            call(
                "browser_fill",
                {
                    "locator": {"strategy": "label", "value": "Profile name"},
                    "value": "Alice",
                },
                "submit-fill",
            ),
            call(
                "browser_click",
                {"locator": locator("button", "Submit profile")},
                "submit-click",
            ),
            call(
                "browser_extract_text",
                {"locator": {"strategy": "css", "value": "#submit-status"}},
                "submit-extract",
            ),
            finish("submit-finish"),
        ]
    )
    registry = ToolRegistry()
    async with BrowserToolProvider(browser_config("submit_approve")) as provider:
        provider.register_tools(registry)
        graph = make_graph(registry, model)
        config = {"configurable": {"thread_id": "browser-submit-approve"}}
        paused = await graph.ainvoke(
            initial_state("browser-submit-approve"), config=config
        )
        before = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#submit-status"}},
        )
        interrupts = interrupt_values(graph, config)
        final = await graph.ainvoke(
            Command(resume={"decision": "approve"}),
            config=config,
        )
        after = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#submit-status"}},
        )

    assert paused["status"] is TaskStatus.WAITING_APPROVAL
    assert before["text"] == "Profile not submitted."
    assert interrupts[0].value["risk_level"] == "high"
    assert after["text"] == "Profile submitted."
    assert final["status"] is TaskStatus.COMPLETED
    assert [record.tool_name for record in final["tool_calls"]].count(
        "browser_click"
    ) == 1
    print(
        "BROWSER_SUBMIT_APPROVAL_SMOKE="
        + json.dumps(
            {
                "status_before": before["text"],
                "paused_status": paused["status"].value,
                "interrupt": interrupts[0].value,
                "status_after": after["text"],
                "final_status": final["status"].value,
            },
            ensure_ascii=True,
        )
    )


@pytest.mark.asyncio
async def test_browser_submit_rejection_never_changes_dom(
    browser_site_url: str,
) -> None:
    model = FakeFunctionCallingModel(
        [
            call("browser_navigate", {"url": browser_site_url}, "reject-nav"),
            call(
                "browser_click",
                {"locator": locator("button", "Submit profile")},
                "reject-submit",
            ),
            call(
                "browser_extract_text",
                {"locator": {"strategy": "css", "value": "#submit-status"}},
                "reject-extract",
            ),
            finish("reject-finish"),
        ]
    )
    registry = ToolRegistry()
    async with BrowserToolProvider(browser_config("submit_reject")) as provider:
        provider.register_tools(registry)
        graph = make_graph(registry, model)
        config = {"configurable": {"thread_id": "browser-submit-reject"}}
        paused = await graph.ainvoke(
            initial_state("browser-submit-reject"), config=config
        )
        final = await graph.ainvoke(
            Command(resume={"decision": "reject", "reason": "Do not submit"}),
            config=config,
        )
        status = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#submit-status"}},
        )

    assert paused["status"] is TaskStatus.WAITING_APPROVAL
    assert status["text"] == "Profile not submitted."
    assert final["status"] is TaskStatus.COMPLETED
    assert "browser_click" not in [record.tool_name for record in final["tool_calls"]]


@pytest.mark.asyncio
async def test_browser_delete_is_high_and_only_changes_dom_after_approval(
    browser_site_url: str,
) -> None:
    model = FakeFunctionCallingModel(
        [
            call("browser_navigate", {"url": browser_site_url}, "delete-nav"),
            call(
                "browser_click",
                {"locator": locator("button", "Delete item")},
                "delete-click",
            ),
            finish("delete-finish"),
        ]
    )
    registry = ToolRegistry()
    async with BrowserToolProvider(browser_config("delete")) as provider:
        provider.register_tools(registry)
        graph = make_graph(registry, model)
        config = {"configurable": {"thread_id": "browser-delete"}}
        paused = await graph.ainvoke(initial_state("browser-delete"), config=config)
        before = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#delete-status"}},
        )
        interrupts = interrupt_values(graph, config)
        final = await graph.ainvoke(
            Command(resume={"decision": "approve"}),
            config=config,
        )
        after = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#delete-status"}},
        )

    assert paused["status"] is TaskStatus.WAITING_APPROVAL
    assert interrupts[0].value["risk_level"] == "high"
    assert before["text"] == "Item exists."
    assert after["text"] == "Item deleted."
    assert final["status"] is TaskStatus.COMPLETED
    print(
        "BROWSER_DELETE_APPROVAL_SMOKE="
        + json.dumps(
            {
                "before": before["text"],
                "risk": interrupts[0].value["risk_level"],
                "after": after["text"],
            },
            ensure_ascii=True,
        )
    )
```

### `tests/test_browser.py`

```python
"""Phase 7 DOM-first Browser Tools 的真实 Chromium 测试。"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from mcp import ClientSession
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from taskpilot.browser import (
    BrowserConfig,
    BrowserDownloadError,
    BrowserLocatorError,
    BrowserNavigationError,
    BrowserPageError,
    BrowserToolProvider,
    LocatorSpec,
)
from taskpilot.browser.tools import BrowserTools
from taskpilot.mcp import MCPToolAdapter
from taskpilot.tools import ToolRegistry


def _browser_config(*, snapshot_chars: int = 20_000) -> BrowserConfig:
    root = Path("tests/.artifacts/browser").resolve()
    return BrowserConfig(
        headless=True,
        action_timeout_ms=3_000,
        navigation_timeout_ms=5_000,
        download_dir=root / "downloads",
        artifact_dir=root / "screenshots",
        max_snapshot_chars=snapshot_chars,
    )


def test_locator_spec_rejects_incomplete_semantic_locator() -> None:
    with pytest.raises(ValidationError, match="role 和 name"):
        LocatorSpec(strategy="role", role="button")

    with pytest.raises(ValidationError, match="必须提供 value"):
        LocatorSpec(strategy="css")


def test_download_filename_sanitization_blocks_path_traversal() -> None:
    assert BrowserTools._sanitize_filename("../danger report.txt") == (
        "danger_report.txt"
    )
    with pytest.raises(BrowserDownloadError, match="无法安全保存"):
        BrowserTools._sanitize_filename("..")


@pytest.mark.asyncio
async def test_real_browser_tools_cover_dom_pages_iframe_download_and_cleanup(
    browser_site_url: str,
) -> None:
    registry = ToolRegistry()
    provider = BrowserToolProvider(_browser_config())

    async with provider:
        tools = provider.register_tools(registry)
        assert len(tools) == 11
        assert provider.session.browser_version
        assert provider.tool_names == [tool.name for tool in tools]
        schemas = registry.list_function_schemas()
        assert {schema["function"]["name"] for schema in schemas} == set(
            provider.tool_names
        )
        initial_page = provider.session.get_page("page-1")[1]

        navigation = await registry.invoke(
            "browser_navigate", {"url": f"{browser_site_url}/index.html"}
        )
        assert navigation["page_id"] == "page-1"
        assert navigation["status"] == 200
        assert navigation["title"] == "TaskPilot Browser Fixture"

        observation = await registry.invoke("browser_observe", {})
        snapshot = observation["aria_snapshot"]
        assert observation["snapshot_mode"] == "ai"
        assert "TaskPilot Browser Fixture" in snapshot
        assert "Search Query" in snapshot
        assert "Apply filters" in snapshot
        assert observation["truncated"] is False
        # Tool observation 必须能直接进入 ToolCallRecord/ToolMessage JSON。
        json.dumps(observation, ensure_ascii=False)

        await registry.invoke(
            "browser_fill",
            {
                "locator": {"strategy": "label", "value": "Search Query"},
                "value": "Hong Kong gym",
            },
        )
        await registry.invoke(
            "browser_select",
            {
                "locator": {"strategy": "label", "value": "Category"},
                "value": "gyms",
            },
        )
        checked = await registry.invoke(
            "browser_check",
            {
                "locator": {
                    "strategy": "label",
                    "value": "Include inactive",
                }
            },
        )
        assert checked["checked"] is True
        await registry.invoke(
            "browser_click",
            {
                "locator": {
                    "strategy": "role",
                    "role": "button",
                    "name": "Apply filters",
                }
            },
        )
        results = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#results"}},
        )
        assert "Hong Kong gym" in results["text"]
        assert "Harbor Gym" in results["text"]

        css_fallback = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#css-fallback"}},
        )
        assert css_fallback["text"] == "CSS fallback target"

        with pytest.raises(BrowserLocatorError, match="strict mode violation"):
            await registry.invoke(
                "browser_click",
                {
                    "locator": {
                        "strategy": "role",
                        "role": "button",
                        "name": "Duplicate action",
                    }
                },
            )

        # Locator action 自己 auto-wait，不能被提前 locator.count()==0 截断。
        await registry.invoke(
            "browser_fill",
            {
                "locator": {"strategy": "label", "value": "Delayed Input"},
                "value": "appeared later",
            },
        )
        await registry.invoke(
            "browser_click",
            {
                "locator": {
                    "strategy": "role",
                    "role": "button",
                    "name": "Delayed Apply",
                }
            },
        )
        delayed = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#delayed-status"}},
        )
        assert delayed["text"] == "Delayed value: appeared later"

        frame_chain = ["iframe[title='Demo frame']"]
        await registry.invoke(
            "browser_fill",
            {
                "locator": {
                    "strategy": "label",
                    "value": "Frame Input",
                    "frame_chain": frame_chain,
                },
                "value": "inside iframe",
            },
        )
        await registry.invoke(
            "browser_click",
            {
                "locator": {
                    "strategy": "role",
                    "role": "button",
                    "name": "Update frame",
                    "frame_chain": frame_chain,
                }
            },
        )
        frame_result = await registry.invoke(
            "browser_extract_text",
            {
                "locator": {
                    "strategy": "css",
                    "value": "#frame-result",
                    "frame_chain": frame_chain,
                }
            },
        )
        assert frame_result["text"] == "Frame value: inside iframe"
        with pytest.raises(BrowserLocatorError, match="page_url="):
            await registry.invoke(
                "browser_fill",
                {
                    "locator": {
                        "strategy": "label",
                        "value": "Frame Input",
                        "frame_chain": ["iframe[title='Missing frame']"],
                    },
                    "value": "unreachable",
                },
            )

        popup = await registry.invoke(
            "browser_click",
            {
                "locator": {
                    "strategy": "role",
                    "role": "link",
                    "name": "Open details popup",
                },
                "expect_popup": True,
            },
        )
        assert popup["new_page_id"] == "page-2"
        pages = await registry.invoke("browser_list_pages", {})
        assert [page["page_id"] for page in pages["pages"]] == [
            "page-1",
            "page-2",
        ]
        switched = await registry.invoke(
            "browser_switch_page", {"page_id": "page-2"}
        )
        assert switched["title"] == "TaskPilot Details"
        details = await registry.invoke("browser_extract_text", {})
        assert "HKD 420" in details["text"]

        await registry.invoke("browser_switch_page", {"page_id": "page-1"})
        downloaded = await registry.invoke(
            "browser_download",
            {
                "locator": {
                    "strategy": "role",
                    "role": "link",
                    "name": "Download report",
                }
            },
        )
        download_path = Path(downloaded["saved_path"])
        assert download_path.parent == provider.config.download_dir
        assert download_path.read_text(encoding="utf-8").strip() == (
            "TaskPilot controlled browser download fixture."
        )

        screenshot = await registry.invoke(
            "browser_screenshot", {"full_page": True}
        )
        screenshot_path = Path(screenshot["saved_path"])
        assert screenshot_path.parent == provider.config.artifact_dir
        assert screenshot_path.suffix == ".png"
        assert screenshot_path.stat().st_size > 0

        for unsafe_url in ("javascript:alert(1)", "data:text/plain,unsafe"):
            with pytest.raises(BrowserNavigationError, match="非 HTTP"):
                await registry.invoke("browser_navigate", {"url": unsafe_url})
        with pytest.raises(BrowserPageError, match="未知或已关闭"):
            await registry.invoke("browser_observe", {"page_id": "page-999"})

        # 关闭当前 popup 后，稳定 ID 映射会清理并回退到仍存活的 page-1。
        await registry.invoke("browser_switch_page", {"page_id": "page-2"})
        await provider.session.get_page("page-2")[1].close()
        remaining = await registry.invoke("browser_list_pages", {})
        assert remaining["pages"] == [
            {
                "page_id": "page-1",
                "active": True,
                "title": "TaskPilot Browser Fixture",
                "url": f"{browser_site_url}/index.html",
            }
        ]
        print(
            "BROWSER_SMOKE="
            + json.dumps(
                {
                    "browser_version": provider.session.browser_version,
                    "navigation": navigation,
                    "semantic_result": results,
                    "iframe_result": frame_result,
                    "popup": popup,
                    "download": downloaded,
                    "screenshot": screenshot,
                    "remaining_pages": remaining["pages"],
                },
                ensure_ascii=False,
            )
        )

    assert provider.session.is_started is False
    assert initial_page.is_closed()


@pytest.mark.asyncio
async def test_observation_reports_explicit_truncation(
    browser_site_url: str,
) -> None:
    registry = ToolRegistry()
    async with BrowserToolProvider(
        _browser_config(snapshot_chars=120)
    ) as provider:
        provider.register_tools(registry)
        await registry.invoke("browser_navigate", {"url": browser_site_url})
        observation = await registry.invoke("browser_observe", {})

    assert observation["truncated"] is True
    assert len(observation["aria_snapshot"]) == 120
    assert observation["original_length"] > 120


@pytest.mark.asyncio
async def test_locator_actions_auto_wait_for_delayed_dom(
    browser_site_url: str,
) -> None:
    """晚出现元素由 Locator.fill/click auto-wait；测试不使用 sleep。"""

    registry = ToolRegistry()
    async with BrowserToolProvider(_browser_config()) as provider:
        provider.register_tools(registry)
        await registry.invoke(
            "browser_navigate",
            {"url": f"{browser_site_url}/delayed.html"},
        )
        await registry.invoke(
            "browser_fill",
            {
                "locator": {"strategy": "label", "value": "Late Input"},
                "value": "auto-wait works",
            },
        )
        await registry.invoke(
            "browser_click",
            {
                "locator": {
                    "strategy": "role",
                    "role": "button",
                    "name": "Late Apply",
                }
            },
        )
        result = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#delayed-result"}},
        )

    assert result["text"] == "Late value: auto-wait works"


@dataclass
class _LocalEchoTool:
    name: str = "local_echo"
    description: str = "Return an offline value."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }
    )

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"value": arguments["value"]}


class _FakeMCPSession:
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        return CallToolResult(content=[TextContent(text=f"{name}:{arguments['q']}")])


@pytest.mark.asyncio
async def test_browser_mcp_and_local_tools_share_one_registry() -> None:
    registry = ToolRegistry()
    registry.register(_LocalEchoTool())
    registry.register(
        MCPToolAdapter(
            name="demo__lookup",
            remote_tool_name="lookup",
            server_id="demo",
            description="Fake MCP lookup",
            input_schema={
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
                "additionalProperties": False,
            },
            session=cast(ClientSession, _FakeMCPSession()),
        )
    )
    async with BrowserToolProvider(_browser_config()) as provider:
        provider.register_tools(registry)
        names = {tool.name for tool in registry.list_tools()}
        local_result = await registry.invoke("local_echo", {"value": "ok"})
        mcp_result = await registry.invoke("demo__lookup", {"q": "gym"})

    assert "browser_observe" in names
    assert {"local_echo", "demo__lookup"}.issubset(names)
    assert local_result == {"value": "ok"}
    assert mcp_result["content"][0]["text"] == "lookup:gym"
```

### `tests/test_mcp_integration.py`

```python
"""官方 MCP SDK + 真实本地 STDIO subprocess 的 Phase 6 集成测试。"""

import json
import sys
from pathlib import Path
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.mcp import (
    MCPServerConfig,
    MCPToolExecutionError,
    MCPToolProvider,
)
from taskpilot.models import PlanStep, TaskSpec, ToolCallStatus
from taskpilot.policy import DefaultRiskPolicy, PolicyOutcome, RiskLevel
from taskpilot.tools import ToolRegistry


SERVER_PATH = Path(__file__).parent / "fixtures" / "mcp_server.py"


def make_stdio_config(*, trust_tool_annotations: bool = False) -> MCPServerConfig:
    """使用当前 pytest 解释器启动同一环境中的官方 MCP Server。"""

    return MCPServerConfig(
        server_id="demo",
        transport="stdio",
        command=sys.executable,
        args=["-u", str(SERVER_PATH)],
        trust_tool_annotations=trust_tool_annotations,
    )


@pytest.mark.asyncio
async def test_real_stdio_discovery_registration_call_and_shutdown() -> None:
    registry = ToolRegistry()
    provider = MCPToolProvider([make_stdio_config()])

    async with provider:
        adapters = await provider.register_tools(registry)
        discovered = {
            item.remote_tool_name: item.public_tool_name
            for item in provider.discovery_info
        }

        assert provider.connected_server_ids == ["demo"]
        assert set(discovered) == {
            "echo",
            "add",
            "destructive_dummy",
            "fail_tool",
        }
        assert discovered["echo"] == "demo__echo"
        assert discovered["add"] == "demo__add"
        assert {adapter.name for adapter in adapters} == set(discovered.values())

        schemas = {
            item["function"]["name"]: item["function"]["parameters"]
            for item in registry.list_function_schemas()
        }
        assert schemas["demo__echo"]["required"] == ["text"]
        assert set(schemas["demo__add"]["required"]) == {"a", "b"}

        result = await registry.invoke("demo__add", {"a": 2, "b": 3})
        assert result["structured_content"] == {"sum": 5}
        assert result["is_error"] is False

        echo_adapter = next(item for item in adapters if item.name == "demo__echo")
        assert echo_adapter.remote_tool_name == "echo"
        assert echo_adapter.server_id == "demo"
        assert echo_adapter.metadata["annotations"]["read_only_hint"] is True
        assert echo_adapter.metadata["annotations"]["destructive_hint"] is False
        print(
            "STDIO_DISCOVERY_SMOKE="
            + json.dumps(
                {
                    "connected": provider.connected_server_ids,
                    "discovered": discovered,
                    "add_required": schemas["demo__add"]["required"],
                    "add_result": result,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )

    assert provider.connected_server_ids == []


@pytest.mark.asyncio
async def test_real_mcp_annotation_trust_policy() -> None:
    policy = DefaultRiskPolicy()
    async with MCPToolProvider(
        [make_stdio_config(trust_tool_annotations=True)]
    ) as trusted_provider:
        trusted = {
            tool.remote_tool_name: tool
            for tool in await trusted_provider.discover_tools()
        }
        read_decision = await policy.assess(
            call_id="trusted-read",
            step_id=1,
            tool=trusted["echo"],
            arguments={"text": "hello"},
        )
        destructive_decision = await policy.assess(
            call_id="trusted-destructive",
            step_id=1,
            tool=trusted["destructive_dummy"],
            arguments={"target": "fixture"},
        )

    async with MCPToolProvider(
        [make_stdio_config(trust_tool_annotations=False)]
    ) as untrusted_provider:
        untrusted = {
            tool.remote_tool_name: tool
            for tool in await untrusted_provider.discover_tools()
        }
        untrusted_read = await policy.assess(
            call_id="untrusted-read",
            step_id=1,
            tool=untrusted["echo"],
            arguments={"text": "hello"},
        )

    assert read_decision.risk_level is RiskLevel.LOW
    assert read_decision.outcome is PolicyOutcome.ALLOW
    assert read_decision.annotations_trusted is True
    assert destructive_decision.risk_level is RiskLevel.HIGH
    assert destructive_decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert untrusted_read.risk_level is RiskLevel.HIGH
    assert untrusted_read.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert untrusted_read.annotations_trusted is False
    print(
        "MCP_POLICY_TRUST_SMOKE="
        + json.dumps(
            {
                "trusted_read": read_decision.risk_level.value,
                "trusted_destructive": destructive_decision.risk_level.value,
                "untrusted_read": untrusted_read.risk_level.value,
            },
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
async def test_real_stdio_mcp_is_error_is_not_tool_success() -> None:
    registry = ToolRegistry()

    async with MCPToolProvider([make_stdio_config()]) as provider:
        await provider.register_tools(registry)

        with pytest.raises(MCPToolExecutionError) as error:
            await registry.invoke("demo__fail_tool", {})

        assert error.value.result["is_error"] is True
        assert "intentional MCP failure" in str(error.value)
        print(
            "MCP_ERROR_SMOKE="
            + json.dumps(
                {
                    "public_tool_name": "demo__fail_tool",
                    "raised": type(error.value).__name__,
                    "is_error": error.value.result["is_error"],
                    "content": error.value.result["content"],
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )


class FakeAsyncFunctionCallingModel:
    """给真实 async Executor 提供可预测的原生 Tool Calls。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.bound_tools: list[dict[str, Any]] = []
        self.invocations: list[list[BaseMessage]] = []

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> "FakeAsyncFunctionCallingModel":
        self.bound_tools = list(tools)
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations.append(list(messages))
        return self.responses.pop(0)


def tool_call(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": arguments,
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def finish_call(call_id: str) -> AIMessage:
    return tool_call(
        FINISH_STEP_NAME,
        {
            "summary": "The real MCP echo result was observed.",
            "evidence": ["demo__echo returned structured_content"],
            "output": {"echoed": "hello-mcp"},
        },
        call_id,
    )


def make_step() -> PlanStep:
    return PlanStep(
        id=1,
        description="Echo a value through MCP",
        success_criteria=["The echoed value is observed"],
    )


@pytest.mark.asyncio
async def test_executor_registry_real_mcp_end_to_end() -> None:
    registry = ToolRegistry()
    model = FakeAsyncFunctionCallingModel(
        [
            tool_call("demo__echo", {"text": "hello-mcp"}, "mcp-call-1"),
            finish_call("finish-call-2"),
        ]
    )

    async with MCPToolProvider([make_stdio_config()]) as provider:
        await provider.register_tools(registry)
        executor = TaskExecutor(
            registry=registry,
            model=cast(BaseChatModel, model),
        )
        result = await executor.execute(
            task_spec=TaskSpec(goal="Echo through real MCP"),
            step=make_step(),
            task_results={},
            tool_calls=[],
        )

    assert result.outcome.claimed_complete is True
    assert result.outcome.output == {"echoed": "hello-mcp"}
    assert result.fatal_error is False
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].call_id == "mcp-call-1"
    assert result.tool_calls[0].tool_name == "demo__echo"
    assert result.tool_calls[0].status is ToolCallStatus.SUCCESS
    assert result.tool_calls[0].result["structured_content"] == {
        "echoed": "hello-mcp"
    }

    second_round = model.invocations[1]
    tool_message = second_round[-1]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "mcp-call-1"
    assert tool_message.status == "success"
    observation = json.loads(str(tool_message.content))
    assert observation["result"]["structured_content"] == {
        "echoed": "hello-mcp"
    }
    print(
        "EXECUTOR_MCP_SMOKE="
        + json.dumps(
            {
                "tool_name": result.tool_calls[0].tool_name,
                "call_id": result.tool_calls[0].call_id,
                "record_status": result.tool_calls[0].status.value,
                "tool_message_call_id": tool_message.tool_call_id,
                "tool_message_status": tool_message.status,
                "structured_content": observation["result"][
                    "structured_content"
                ],
                "claimed_complete": result.outcome.claimed_complete,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
async def test_mcp_error_becomes_failed_record_and_executor_continues() -> None:
    registry = ToolRegistry()
    model = FakeAsyncFunctionCallingModel(
        [
            tool_call("demo__fail_tool", {}, "mcp-fail-1"),
            tool_call("demo__echo", {"text": "hello-mcp"}, "mcp-echo-2"),
            finish_call("finish-after-error-3"),
        ]
    )

    async with MCPToolProvider([make_stdio_config()]) as provider:
        await provider.register_tools(registry)
        executor = TaskExecutor(
            registry=registry,
            model=cast(BaseChatModel, model),
        )
        result = await executor.execute(
            task_spec=TaskSpec(goal="Recover from one MCP tool error"),
            step=make_step(),
            task_results={},
            tool_calls=[],
        )

    assert result.outcome.claimed_complete is True
    assert result.fatal_error is False
    assert [record.status for record in result.tool_calls] == [
        ToolCallStatus.FAILED,
        ToolCallStatus.SUCCESS,
    ]
    assert "is_error=true" in result.tool_calls[0].error
    assert "intentional MCP failure" in result.tool_calls[0].error
    error_message = model.invocations[1][-1]
    assert isinstance(error_message, ToolMessage)
    assert error_message.tool_call_id == "mcp-fail-1"
    assert error_message.status == "error"
    assert "intentional MCP failure" in str(error_message.content)
    print(
        "EXECUTOR_MCP_ERROR_RECOVERY_SMOKE="
        + json.dumps(
            {
                "record_statuses": [
                    record.status.value for record in result.tool_calls
                ],
                "error_tool_message_status": error_message.status,
                "executor_continued": result.outcome.claimed_complete,
                "fatal_error": result.fatal_error,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
```

## 28. Pytest Actual Result

命令：

```powershell
conda run -n ai python -m pytest -q
```

真实输出：

```text
........................................................................ [ 62%]
............................................                             [100%]
116 passed in 18.94s
```

结果：116 passed，0 failed。Phase 1–7 的 Analyzer/Planner/Executor/Verifier/MCP/Browser 测试与 Phase 8 测试共同通过。

## 29. Real Graph Interrupt Smoke

命令：

```powershell
conda run -n ai python -m pytest tests/test_hitl.py -q -s
```

真实输出：

```text
HITL_APPROVE_SMOKE={"paused_status": "waiting_approval", "interrupt": {"type": "tool_approval", "call_id": "send-1", "step_id": 1, "tool_name": "send_message", "risk_level": "high", "reason": "Host \u663e\u5f0f Local Tool risk metadata", "arguments_preview": {"recipient": "alice", "message": "hello"}, "tool_source": "local"}, "counter_before": 0, "counter_after": 1, "actual_arguments": {"recipient": "alice", "message": "hello"}, "final_status": "completed"}
.HITL_REJECT_ALTERNATIVE_SMOKE={"high_risk_calls": 0, "low_risk_calls": 1, "approval": {"call_id": "write-1", "step_id": 1, "tool_name": "local_write", "decision": "reject", "reason": "Use read-only path"}, "final_status": "completed"}
....
5 passed in 0.73s
```

测试使用真实 compiled StateGraph、MemorySaver、thread_id、interrupt 和 Command(resume)，没有 mock interrupt 或 input。

## 30. Approve Smoke

`HITL_APPROVE_SMOKE` 展示 paused_status=waiting_approval、counter_before=0、counter_after=1、final_status=completed。最终 Tool 实际收到 `{"recipient":"alice","message":"hello"}`，与 PendingToolAction 完全一致。

## 31. Reject Smoke

`HITL_REJECT_ALTERNATIVE_SMOKE` 展示 high_risk_calls=0、low_risk_calls=1、approval=reject、final_status=completed。Human rejection 作为 Executor context feedback，不等于 Task failure。

## 32. Exactly-once Counter Smoke

一次正常 interrupt → approve resume 中，高风险 side-effect counter 从 0 变为 1，Graph 完成后仍为 1。Phase 8 不声称解决 crash/restart replay；这里只保证正常单次 resume 流程。

## 33. Browser Submit Approval Smoke

命令：

```powershell
conda run -n ai python -m pytest tests/test_browser_policy.py -q -s
```

真实输出：

```text
..BROWSER_SUBMIT_APPROVAL_SMOKE={"status_before": "Profile not submitted.", "paused_status": "waiting_approval", "interrupt": {"type": "tool_approval", "call_id": "submit-click", "step_id": 1, "tool_name": "browser_click", "risk_level": "high", "reason": "\u76ee\u6807\u5177\u6709 HTML form submit \u8bed\u4e49", "arguments_preview": {"locator": {"strategy": "role", "role": "button", "name": "Submit profile"}}, "tool_source": "browser"}, "status_after": "Profile submitted.", "final_status": "completed"}
..BROWSER_DELETE_APPROVAL_SMOKE={"before": "Item exists.", "risk": "high", "after": "Item deleted."}
.
5 passed in 3.63s
```

Submit profile 是真实 `button type=submit`。审批前 DOM 为 `Profile not submitted.`，interrupt risk=high，Approve 后才变为 `Profile submitted.`。Reject 测试中 DOM 始终未提交，ToolCallRecords 不含 browser_click。

## 34. Browser Delete Approval Smoke

非 form 的 `Delete item` 因破坏性 accessible name 判为 HIGH。审批前 `Item exists.`，批准后 `Item deleted.`；preflight 自身不改变 DOM。

## 35. MCP Trusted / Untrusted Annotation Smoke

命令：

```powershell
conda run -n ai python -m pytest tests/test_mcp_integration.py::test_real_mcp_annotation_trust_policy -q -s
```

真实输出：

```text
MCP_POLICY_TRUST_SMOKE={"trusted_destructive": "high", "trusted_read": "low", "untrusted_read": "high"}
.
1 passed in 2.21s
```

真实 STDIO discovery 的 trusted echo → LOW，trusted destructive_dummy → HIGH，untrusted echo 即使声明 read-only 仍 → HIGH。

## 36. Sensitive Preview Smoke

测试输入包含 api_key 和 nested password。interrupt preview 只含 `[REDACTED]`，pytest smoke 不打印 secret；Approve 后真实 Tool 内部断言收到原始值。另一个非法 resume 携带 edited arguments 时 status=FAILED、counter=0。

## 37. Known Limitations

- MemorySaver 只在当前进程内恢复，不能跨进程重启。
- 没有 crash recovery、idempotency key/ledger 或 crash exactly-once。
- Keyword 只是 Browser click 的辅助规则；HTML submit semantics 优先，但复杂自定义控件仍可能保守升级为 HIGH。
- Policy preflight 只能读当前 DOM，无法证明远端服务真实行为。
- MCP annotations 是 hints，不是 enforcement 或安全保证。
- LOW/MEDIUM 自动执行是当前默认策略，没有企业级 policy language。
- 没有完整 Prompt Injection detection/hardening；恶意网页仍可能影响模型。
- 没有 Vision/OCR、Dynamic Replanning、FastAPI 或 Benchmark。
- CLI 只有终端 y/N，没有 Web UI。
- Approval 不代表 Step verified 或 Task completed，仍必须经过两层 Verifier。

## 38. Files Changed

Phase 8 新建：

- `src/taskpilot/policy/{__init__,models,browser,engine}.py`：Policy models/rules/engine；
- `tests/fixtures/browser_site/delayed.html`：auto-wait fixture；
- `tests/test_policy.py`：Policy/redaction/DENY tests；
- `tests/test_hitl.py`：真实 interrupt approve/reject/exact-once tests；
- `tests/test_browser_policy.py`：真实 Chromium risk/HITL tests；
- `docs/review/phase_08.md`：本验收文档。

Phase 8 修改：

- `src/taskpilot/browser/tools.py`：auto-wait fix、metadata、click preflight；
- `src/taskpilot/tools/registry.py`：无副作用 schema prevalidation；
- `src/taskpilot/mcp/config.py` / `provider.py`：annotation trust boundary metadata；
- `src/taskpilot/executor.py`：Policy gate、pending、exact approved execution；
- `src/taskpilot/state.py`：Policy/HITL state；
- `src/taskpilot/graph.py`：interrupt/resume nodes and routes；
- `src/taskpilot/cli.py`：MemorySaver/thread_id/approval loop；
- `tests/fixtures/browser_site/index.html`：submit/delete fixture；
- `tests/fixtures/mcp_server.py`：destructive annotation fixture；
- 既有 initial-state/Fake Executor tests：同步 Phase 8 state/signature；
- `README.md`：Phase 8 能力和安全边界。

Phase 8 没有新增 Python dependency。

## 39. Git Status

以下将在文档创建后替换为实际 `git status --short` stdout。此前 Phase 的用户修改全部保留。

```text
 M .gitignore
 M README.md
 M docs/review/phase_03.md
 M pyproject.toml
 M src/taskpilot/cli.py
 M src/taskpilot/graph.py
 M src/taskpilot/models.py
 M src/taskpilot/state.py
 M tests/test_analyzer.py
 M tests/test_graph_smoke.py
 M tests/test_models.py
 M tests/test_planner.py
?? docs/review/phase_04.md
?? docs/review/phase_05.md
?? docs/review/phase_06.md
?? docs/review/phase_07.md
?? docs/review/phase_08.md
?? src/taskpilot/browser/
?? src/taskpilot/executor.py
?? src/taskpilot/mcp/
?? src/taskpilot/policy/
?? src/taskpilot/tools/
?? src/taskpilot/verifier.py
?? tests/conftest.py
?? tests/fixtures/
?? tests/test_browser.py
?? tests/test_browser_executor.py
?? tests/test_browser_policy.py
?? tests/test_executor.py
?? tests/test_hitl.py
?? tests/test_mcp.py
?? tests/test_mcp_integration.py
?? tests/test_phase5_graph.py
?? tests/test_policy.py
?? tests/test_tools.py
?? tests/test_verifier.py
```

## 40. Boundary Checks (Q1–Q15)

### Q1：高风险 Tool 是否可能在 Human Approval 之前执行？

**No。** REQUIRE_APPROVAL 分支在 `registry.invoke` 前返回 PendingToolAction；真实 counter/DOM 测试均为审批前 0/未变化。

### Q2：interrupt 是否直接藏在 ToolRegistry.invoke 里面？

**No。** Registry 只做 lookup/schema/invoke。interrupt 位于独立 `human_approval` Graph node。

### Q3：为什么不能在 Executor 已执行若干副作用后直接 tool 内 interrupt？

LangGraph resume 会从 interrupt 所在 node 开头重放。如果同一个 node 在 interrupt 前已经执行副作用，恢复时可能再次执行。当前 execute_step 先完整返回 pending 并 checkpoint，prepare 单独提交 WAITING_APPROVAL，human node 在 interrupt 前零副作用，获批动作又在后续独立 node 执行，因此避免正常 resume 的前置副作用重放。

### Q4：Approve 后 Tool arguments 会不会重新由 LLM 生成？

**No。** 直接执行 frozen PendingToolAction.tool_name + arguments；resume schema 禁止 edited arguments。

### Q5：Reject 是否会调用 Tool？

**No。** Reject 只写 ApprovalRecord、清 pending 并回到 Executor；counter 和 Browser DOM 测试证明未调用。

### Q6：Reject 是否自动使整个 Task failed？

**No。** status 恢复 RUNNING，模型可用 low-risk alternative 并最终 completed。

### Q7：Approval 是否代表 Step verified？

**No。** Approval 只授权一个 action。Step 仍需 Step Verifier，Task 仍需 Task Verifier。

### Q8：MCP readOnlyHint 是否无条件可信？

**No。** 默认 `trust_tool_annotations=False`；只有 Host 对该 Server 显式开启时才作为 hint。

### Q9：未知 Local/MCP Tool 默认是低风险吗？

**No。** 缺失/未知 metadata 保守 HIGH → REQUIRE_APPROVAL。

### Q10：Browser form submit 是否需要 Approval？

**Yes。** HTML submit semantics deterministic HIGH，真实 Graph 已 interrupt。

### Q11：普通 browser_fill 是否需要每次 Approval？

**No。** 当前分类 MEDIUM，默认 ALLOW。

### Q12：Task 状态暂停时是否为 waiting_approval？

**Yes。** prepare node 在 human interrupt 前先写 WAITING_APPROVAL；真实 smoke 输出展示该值。

### Q13：是否用了真实 LangGraph interrupt/resume？

**Yes。** 第 29 节记录 MemorySaver、thread_id、真实 interrupt snapshot 和 Command resume 测试输出。

### Q14：Phase 8 HITL 是否能跨进程重启恢复？

**No。** 当前是 MemorySaver；Phase 9 才实现 durable Checkpoint/Recovery。

### Q15：Phase 8 是否已经解决 Prompt Injection？

**No。** Policy 只保护 Tool execution boundary，不保证网页内容无法操纵模型。
