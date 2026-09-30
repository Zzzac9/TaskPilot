# TaskPilot Phase 7 Review

> 本文档基于当前工作区最终代码和本机实际命令输出生成，供人工架构与代码验收使用。记录日期：2026-09-01。

## 1. Phase Goal

本阶段目标是把 Playwright async Python API 接入现有统一 async Tool Runtime，形成跨 Tool Call 保持状态、DOM/accessibility-first 的 Chromium 执行环境。

实际完成：

- 长生命周期 `BrowserSession`、稳定 `page_id` 和 active page 管理；
- 11 个 Browser Tools，覆盖导航、观察、点击、表单、提取、iframe、popup、下载和截图；
- Browser、MCP、Local Tool 共用现有 `ToolRegistry`；
- 真实 Chromium + 标准库本地 HTTP fixture 的完全离线测试；
- 真实 `TaskExecutor` 与完整 LangGraph Browser smoke；
- 普通 DOM Locator 失败后的 Observation 恢复路径；
- CLI Host 通过 `AsyncExitStack` 组合 MCP/Browser 生命周期。

明确没有实现 Vision/OCR、Human-in-the-loop、Risk Approval、生产 Web Search、生产 Filesystem Agent、Checkpoint/Recovery、Dynamic Replanning、FastAPI 或 Benchmark execution，也没有开始 Phase 8。

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
│       └── phase_07.md
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
    │   │   ├── details.html
    │   │   ├── download.txt
    │   │   ├── frame.html
    │   │   └── index.html
    │   ├── mcp_config.json
    │   └── mcp_server.py
    ├── test_analyzer.py
    ├── test_browser.py
    ├── test_browser_executor.py
    ├── test_executor.py
    ├── test_graph_smoke.py
    ├── test_mcp.py
    ├── test_mcp_integration.py
    ├── test_models.py
    ├── test_phase5_graph.py
    ├── test_planner.py
    ├── test_tools.py
    └── test_verifier.py
```

忽略项（如 `__pycache__`、`.pytest_cache`、`tests/.artifacts`）未展开；后者是测试生成的截图和下载文件。

## 3. Installed Playwright Version

实际执行：

```powershell
conda run -n ai python -c "import importlib.metadata; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); print('playwright_package='+importlib.metadata.version('playwright')); print('chromium_browser='+b.version); b.close(); p.stop()"
```

实际输出：

```text
playwright_package=1.62.0
chromium_browser=151.0.7922.34
```

当前公开 async API 已实际检查并使用：

- `async_playwright().start()`
- `BrowserType.launch()`
- `Browser.new_context()`
- `BrowserContext.new_page()`
- `Page` / `FrameLocator` / `Locator`
- `Page.aria_snapshot(mode="ai", depth=...)`
- `Page.expect_popup()`
- `Page.expect_download()`

没有使用 Playwright private API。

## 4. Installed Chromium Version

实际版本为 `151.0.7922.34`。浏览器 binary 安装命令：

```powershell
conda run -n ai python -m playwright install chromium
```

Python 包升级/安装命令：

```powershell
conda run -n ai python -m pip install --upgrade playwright
```

最终 Python dependency 为 `playwright>=1.62,<2`，本机解析版本为 `1.62.0`。

## 5. Browser Architecture

```text
Host / CLI
├── optional MCPToolProvider ─┐
├── optional BrowserToolProvider ─┼→ shared ToolRegistry
└── Local Tools ──────────────┘
                                  ↓
                              TaskExecutor
                                  ↓
                              LangGraph
```

Graph 的实际推理流没有增加 Browser infrastructure node：

```text
START → initialize_task → analyze_task → plan_task → execute_step
      → verify_step → advance_step / retry execute_step
      → verify_task → END
```

## 6. 为什么 Browser lifecycle 在 Graph 外

Browser、context、pages、cookies 和下载生命周期属于 Host 资源管理，而不是任务推理状态。CLI 先进入 Provider，再构造 `TaskExecutor(registry)` 并调用 `graph.ainvoke`，Graph 返回后由 `AsyncExitStack` 逆序关闭资源。这样 Browser 不污染 `TaskState`，也不会在每个 node 或每个 Tool Call 重启。

## 7. BrowserConfig

只声明当前真正支持的 `chromium`，配置 headless、action/navigation timeout、viewport、snapshot/extract 上限，以及下载/截图受控目录。目录在 Pydantic validation 时 `resolve()`，在 Session 启动时创建。

### `src/taskpilot/browser/config.py`

```python
"""Playwright Chromium 执行环境的轻量配置。"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class BrowserConfig(BaseModel):
    """Phase 7 实际支持的 Chromium Browser 配置。"""

    browser_type: Literal["chromium"] = "chromium"
    headless: bool = True
    action_timeout_ms: int = Field(default=5_000, ge=1)
    navigation_timeout_ms: int = Field(default=10_000, ge=1)
    viewport_width: int = Field(default=1280, ge=320)
    viewport_height: int = Field(default=720, ge=240)
    download_dir: Path = Path("artifacts/browser/downloads")
    artifact_dir: Path = Path("artifacts/browser/screenshots")
    default_snapshot_depth: int = Field(default=8, ge=1, le=30)
    max_snapshot_chars: int = Field(default=20_000, ge=100)
    max_extract_chars: int = Field(default=20_000, ge=100)

    @field_validator("download_dir", "artifact_dir")
    @classmethod
    def resolve_controlled_directory(cls, value: Path) -> Path:
        """在任何文件写入前固定为绝对目录。"""

        return value.expanduser().resolve()

    def ensure_directories(self) -> None:
        """Browser lifecycle 启动时创建唯一允许写入的目录。"""

        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
```

## 8. BrowserSession 完整源码

### `src/taskpilot/browser/session.py`

```python
"""跨 Tool Calls 保持状态的 Playwright BrowserSession。"""

from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from taskpilot.browser.config import BrowserConfig


class BrowserError(RuntimeError):
    """面向 Executor 的 Browser 环境错误基类。"""


class BrowserSessionError(BrowserError):
    """Browser lifecycle 未启动或启动/关闭失败。"""


class BrowserPageError(BrowserError):
    """page_id 未知或页面已关闭。"""


class BrowserLocatorError(BrowserError):
    """Locator 不存在、歧义或 actionability 失败。"""


class BrowserNavigationError(BrowserError):
    """URL scheme、navigation 或 navigation timeout 失败。"""


class BrowserDownloadError(BrowserError):
    """下载事件、文件名或受控目录写入失败。"""


def concise_playwright_error(exc: Exception, limit: int = 600) -> str:
    """压缩 Playwright 消息，避免把巨型 traceback 作为 Tool observation。"""

    message = " ".join(str(exc).split())
    return message if len(message) <= limit else message[:limit] + "…"


class BrowserSession:
    """一个 Task/Graph 生命周期内复用 BrowserContext、cookies 和 pages。"""

    def __init__(self, config: BrowserConfig) -> None:
        self.config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._pages: dict[str, Page] = {}
        self._page_ids: dict[Page, str] = {}
        self._next_page_number = 1
        self.active_page_id: str | None = None

    @property
    def is_started(self) -> bool:
        return self._context is not None

    @property
    def browser_version(self) -> str | None:
        return self._browser.version if self._browser is not None else None

    async def __aenter__(self) -> "BrowserSession":
        if self.is_started:
            raise BrowserSessionError("BrowserSession 不能重复进入 context")
        self.config.ensure_directories()
        # 每次进入都代表一个新的 Session 生命周期，page ID 从 1 重新开始。
        self._next_page_number = 1
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.config.headless
            )
            self._context = await self._browser.new_context(
                accept_downloads=True,
                viewport={
                    "width": self.config.viewport_width,
                    "height": self.config.viewport_height,
                },
            )
            self._context.set_default_timeout(self.config.action_timeout_ms)
            self._context.set_default_navigation_timeout(
                self.config.navigation_timeout_ms
            )
            self._context.on("page", self.register_page)
            self.register_page(await self._context.new_page())
        except Exception as exc:
            await self._close_resources()
            raise BrowserSessionError(
                f"BrowserSession 启动失败: {concise_playwright_error(exc)}"
            ) from exc
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        await self._close_resources()

    def register_page(self, page: Page) -> str:
        """为新 Page 分配稳定 ID；同一对象重复注册保持原 ID。"""

        existing = self._page_ids.get(page)
        if existing is not None:
            self.active_page_id = existing
            return existing
        page_id = f"page-{self._next_page_number}"
        self._next_page_number += 1
        self._pages[page_id] = page
        self._page_ids[page] = page_id
        self.active_page_id = page_id
        page.on("close", lambda _page: self._remove_page(page))
        return page_id

    def _remove_page(self, page: Page) -> None:
        page_id = self._page_ids.pop(page, None)
        if page_id is None:
            return
        self._pages.pop(page_id, None)
        if self.active_page_id == page_id:
            self.active_page_id = next(iter(self._pages), None)

    def get_page(self, page_id: str | None = None) -> tuple[str, Page]:
        """返回指定或 active Page；未知 ID 清晰失败。"""

        selected_id = page_id or self.active_page_id
        if selected_id is None:
            raise BrowserPageError("BrowserSession 当前没有可用 Page")
        page = self._pages.get(selected_id)
        if page is None or page.is_closed():
            raise BrowserPageError(f"未知或已关闭的 page_id: {selected_id}")
        return selected_id, page

    async def list_pages(self) -> list[dict[str, Any]]:
        """只返回 JSON-serializable 页面元数据。"""

        result: list[dict[str, Any]] = []
        for page_id, page in self._pages.items():
            if page.is_closed():
                continue
            result.append(
                {
                    "page_id": page_id,
                    "active": page_id == self.active_page_id,
                    "title": await page.title(),
                    "url": page.url,
                }
            )
        return result

    def switch_page(self, page_id: str) -> Page:
        """切换 active Page，不依赖 context.pages 数组位置。"""

        _, page = self.get_page(page_id)
        self.active_page_id = page_id
        return page

    async def _close_resources(self) -> None:
        context, browser, playwright = (
            self._context,
            self._browser,
            self._playwright,
        )
        self._context = None
        self._browser = None
        self._playwright = None
        self._pages.clear()
        self._page_ids.clear()
        self.active_page_id = None
        # 即使前一层关闭异常，也继续释放后续进程资源。
        try:
            if context is not None:
                await context.close()
        finally:
            try:
                if browser is not None:
                    await browser.close()
            finally:
                if playwright is not None:
                    await playwright.stop()
```

## 9. Page ID Lifecycle

- 初始 Page 注册为 `page-1`；
- BrowserContext 的 `page` event 为新 tab/popup 分配单调递增、当前 Session 内稳定的 ID；
- 同一个 Playwright Page 重复注册不会换 ID；
- `active_page_id` 总是指向选中或最新页面；
- close event 清理双向映射；关闭 active page 时回退到仍存活页面；
- 未知/已关闭 ID 抛出 `BrowserPageError`；
- ID 不跨 BrowserSession 持久化。

真实 popup smoke 返回了 `new_page_id: page-2`，关闭它后 page-1 自动恢复 active。

## 10. LocatorSpec 完整定义

### `src/taskpilot/browser/locators.py`

```python
"""语义优先的 Playwright LocatorSpec 与公开 API 映射。"""

from enum import Enum
from typing import Any

from playwright.async_api import FrameLocator, Locator, Page
from pydantic import BaseModel, Field, model_validator


class LocatorStrategy(str, Enum):
    """从语义定位到 CSS fallback 的有限策略集合。"""

    ROLE = "role"
    LABEL = "label"
    TEST_ID = "test_id"
    TEXT = "text"
    PLACEHOLDER = "placeholder"
    CSS = "css"


class LocatorSpec(BaseModel):
    """LLM 可提供的结构化定位信息；不支持任意 XPath/JavaScript。"""

    strategy: LocatorStrategy
    role: str | None = None
    name: str | None = None
    value: str | None = None
    exact: bool = False
    # frame_chain 只描述 iframe 边界；每一项交给公开 FrameLocator API。
    frame_chain: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_strategy_fields(self) -> "LocatorSpec":
        if self.strategy is LocatorStrategy.ROLE:
            if not self.role or not self.name:
                raise ValueError("role locator 必须提供 role 和 name")
        elif not self.value:
            raise ValueError(f"{self.strategy.value} locator 必须提供 value")
        if any(not selector.strip() for selector in self.frame_chain):
            raise ValueError("frame_chain 不能包含空 selector")
        return self


LOCATOR_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy": {
            "type": "string",
            "enum": [strategy.value for strategy in LocatorStrategy],
        },
        "role": {"type": ["string", "null"]},
        "name": {"type": ["string", "null"]},
        "value": {"type": ["string", "null"]},
        "exact": {"type": "boolean", "default": False},
        "frame_chain": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "default": [],
        },
    },
    "required": ["strategy"],
    "additionalProperties": False,
}


def resolve_locator(page: Page, spec: LocatorSpec) -> Locator:
    """按公开 Locator/FrameLocator API 构造严格 Locator。"""

    target: Page | FrameLocator = page
    for frame_selector in spec.frame_chain:
        target = target.frame_locator(frame_selector)

    if spec.strategy is LocatorStrategy.ROLE:
        return target.get_by_role(
            spec.role,  # type: ignore[arg-type]
            name=spec.name,
            exact=spec.exact,
        )
    if spec.strategy is LocatorStrategy.LABEL:
        return target.get_by_label(spec.value or "", exact=spec.exact)
    if spec.strategy is LocatorStrategy.TEST_ID:
        return target.get_by_test_id(spec.value or "")
    if spec.strategy is LocatorStrategy.TEXT:
        return target.get_by_text(spec.value or "", exact=spec.exact)
    if spec.strategy is LocatorStrategy.PLACEHOLDER:
        return target.get_by_placeholder(spec.value or "", exact=spec.exact)
    return target.locator(spec.value or "")
```

## 11. Locator Priority

Tool description 明确约束：

1. role + accessible name
2. label
3. test_id
4. text / placeholder
5. CSS fallback

Runtime 不生成 `nth-child`，不提供 XPath。每次 action 前调用 `locator.count()` 并要求恰好为 1；不会偷偷使用 `.first`。Playwright `Locator.click/fill/check/select_option` 保持默认 actionability/auto-wait，没有 `force=True`。

## 12. ARIA / DOM Observation Implementation

`browser_observe` 返回 `page_id`、URL、title、AI-mode ARIA snapshot、depth、截断元数据和所有稳定 page metadata。它不返回 `Page`、`Locator` 等 Python object，也不把整页 `page.content()` 塞进 Observation。测试使用 `json.dumps(observation)` 验证结果可序列化。

## 13. 当前是否使用 AI aria_snapshot

是。当前公开 API 支持 `Page.aria_snapshot(mode="ai", depth=...)`，代码直接使用它。真实输出包含 heading、textbox accessible name、button、link 和 iframe 内语义。

AI snapshot 中的 `[ref=e...]` 当前仅作为 Observation 辅助。Python Playwright 公共 API 没有在本实现中提供安全的 ref-to-locator action 映射，因此没有使用 private/internal selector engine；action 仍由 `LocatorSpec` 走公开 semantic Locator API。

## 14. Snapshot Truncation

默认 depth 为 8，最大 snapshot 为 20,000 字符。超过上限时确定性切片，并同时返回 `truncated=true` 和 `original_length`；不会静默截断。专门测试把上限设为 120 并验证上述字段。

## 15. iframe Implementation

`LocatorSpec.frame_chain` 是 iframe CSS selector 链，只用于公开 `frame_locator()` 跨 frame。进入目标 frame 后仍使用 role/label/test_id/text/placeholder/CSS Locator。真实 fixture 完成：

```text
label("Frame Input") → fill("inside iframe")
role(button, "Update frame") → click
css("#frame-result") → "Frame value: inside iframe"
```

没有使用 ElementHandle 或 `page.evaluate` 绕过 iframe Locator。

## 16. Browser Tools 完整源码

### `src/taskpilot/browser/tools.py`

```python
"""DOM-first Playwright Browser Tools 与长生命周期 Provider。"""

import re
from dataclasses import dataclass
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

    async def invoke(self, arguments: dict[str, Any]) -> Any:
        return await self._handler(arguments)


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
            ),
            BrowserTool(
                "browser_check",
                f"Check one checkbox/radio locator. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {"locator": locator, "page_id": page_id},
                    ["locator"],
                ),
                self.check,
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
            ),
            BrowserTool(
                "browser_list_pages",
                "List stable page IDs with active state, title and URL.",
                _object_schema({}),
                self.list_pages,
            ),
            BrowserTool(
                "browser_switch_page",
                "Switch the active page using a stable page_id.",
                _object_schema({"page_id": page_id}, ["page_id"]),
                self.switch_page,
            ),
            BrowserTool(
                "browser_download",
                f"Click one locator and save its download only in the controlled directory. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {"locator": locator, "page_id": page_id},
                    ["locator"],
                ),
                self.download,
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
        spec, locator = await self._strict_locator(page, arguments["locator"], "click")
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
        spec, locator = await self._strict_locator(page, arguments["locator"], "fill")
        try:
            await locator.fill(arguments["value"])
            return {"filled": True, "page_id": page_id, "value": arguments["value"]}
        except PlaywrightError as exc:
            raise self._locator_action_error("browser_fill", page.url, spec, exc) from exc

    async def select(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = await self._strict_locator(page, arguments["locator"], "select")
        try:
            selected = await locator.select_option(value=arguments["value"])
            return {"selected": selected, "page_id": page_id}
        except PlaywrightError as exc:
            raise self._locator_action_error("browser_select", page.url, spec, exc) from exc

    async def check(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = await self._strict_locator(page, arguments["locator"], "check")
        try:
            await locator.check()
            return {"checked": await locator.is_checked(), "page_id": page_id}
        except PlaywrightError as exc:
            raise self._locator_action_error("browser_check", page.url, spec, exc) from exc

    async def extract_text(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        try:
            if "locator" in arguments:
                _, locator = await self._strict_locator(
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
        spec, locator = await self._strict_locator(page, arguments["locator"], "download")
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

    async def _strict_locator(
        self,
        page: Page,
        raw_spec: dict[str, Any],
        action: str,
    ) -> tuple[LocatorSpec, Locator]:
        try:
            spec = LocatorSpec.model_validate(raw_spec)
            locator = resolve_locator(page, spec)
            count = await locator.count()
        except Exception as exc:
            if isinstance(exc, BrowserError):
                raise
            raise BrowserLocatorError(
                f"browser_{action} locator invalid; page_url={page.url}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc
        if count != 1:
            raise BrowserLocatorError(
                f"browser_{action} locator must match exactly one element; "
                f"page_url={page.url}; strategy={spec.strategy.value}; count={count}"
            )
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

## 17. BrowserToolProvider

`BrowserToolProvider` 位于上面 `tools.py` 的末尾。它只负责 Session lifecycle、构建 11 个固定命名 Tool、冲突检查和注册；不调用 LLM、不参与 Planner/Verifier、不修改 TaskState。公开 Browser 包导出如下：

### `src/taskpilot/browser/__init__.py`

```python
"""基于 Playwright async API 的 DOM-first Browser Tool infrastructure。"""

from taskpilot.browser.config import BrowserConfig
from taskpilot.browser.locators import LocatorSpec, LocatorStrategy
from taskpilot.browser.session import (
    BrowserError,
    BrowserLocatorError,
    BrowserDownloadError,
    BrowserNavigationError,
    BrowserPageError,
    BrowserSession,
    BrowserSessionError,
)
from taskpilot.browser.tools import BrowserToolProvider

__all__ = [
    "BrowserConfig",
    "BrowserError",
    "BrowserLocatorError",
    "BrowserDownloadError",
    "BrowserNavigationError",
    "BrowserPageError",
    "BrowserSession",
    "BrowserSessionError",
    "BrowserToolProvider",
    "LocatorSpec",
    "LocatorStrategy",
]
```

## 18. Error Taxonomy / Normalization

当前轻量错误分类：

- `BrowserError`：基类；
- `BrowserSessionError`：启动/生命周期；
- `BrowserPageError`：未知或关闭 page；
- `BrowserLocatorError`：无匹配、歧义、actionability；
- `BrowserNavigationError`：scheme/navigation/timeout；
- `BrowserDownloadError`：download/filename/path。

Playwright 错误被压缩到 600 字符，保留动作、当前 URL、strategy 和简洁 detail，原异常仍通过 `raise ... from exc` 保留为 cause。Executor 把普通 Tool exception 记录为 FAILED ToolCallRecord 和 error ToolMessage，然后允许下一轮调整策略。

## 19. Timeout Strategy

BrowserContext 设置统一 default action timeout 和 navigation timeout；navigation 另显式使用配置值。等待依赖 Playwright auto-wait、actionability、`domcontentloaded`、`expect_popup` 和 `expect_download`。代码和测试均没有固定 `sleep`。

## 20. Popup Handling

`browser_click(expect_popup=true)` 使用官方 `page.expect_popup()` event context，同步获得 popup 后等待 `domcontentloaded`，注册稳定 page ID。它不依赖 `context.pages[-1]`。

## 21. Download Handling

`browser_download` 使用 `expect_download`，取 `suggested_filename`，先 `Path(...).name` 去掉目录，再只保留安全字符。最终路径 resolve 后必须直属 `download_dir`；冲突时按 `stem_1.ext`、`stem_2.ext` 单调选择。LLM schema 没有 save path 参数。测试验证文件真实存在、内容正确，Session 关闭后文件仍保留。

## 22. Screenshot Handling

`browser_screenshot` 的文件名由 Runtime 生成，目标只在 `artifact_dir`。返回字符串路径、page ID 和 URL。截图目前只用于调试和未来 Vision groundwork；没有发送给 Vision Model，也没有 OCR 或坐标点击。

## 23. Local HTTP Fixture

测试 server 只用 Python 标准库 `ThreadingHTTPServer`，监听随机 localhost 端口，不访问互联网，也没有引入 FastAPI。

### `tests/conftest.py`

```python
"""测试期共享的纯本地 HTTP fixture。"""

from collections.abc import Generator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    """提供静态测试页面，同时关闭不必要的访问日志。"""

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture(scope="session")
def browser_site_url() -> Generator[str, None, None]:
    """在随机本地端口启动标准库 HTTP server。"""

    site_dir = Path(__file__).parent / "fixtures" / "browser_site"
    handler = partial(_QuietStaticHandler, directory=str(site_dir))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
```

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
    </script>
  </body>
</html>
```

### `tests/fixtures/browser_site/details.html`

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>TaskPilot Details</title>
  </head>
  <body>
    <main>
      <h1>Details Page</h1>
      <p>Harbor Gym costs HKD 420 per month.</p>
      <p>Peak Fitness costs HKD 480 per month.</p>
    </main>
  </body>
</html>
```

### `tests/fixtures/browser_site/frame.html`

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Frame Fixture</title>
  </head>
  <body>
    <label for="frame-input">Frame Input</label>
    <input id="frame-input">
    <button id="frame-update" type="button">Update frame</button>
    <p id="frame-result">Frame waiting.</p>
    <script>
      document.querySelector("#frame-update").addEventListener("click", () => {
        const value = document.querySelector("#frame-input").value;
        document.querySelector("#frame-result").textContent = `Frame value: ${value}`;
      });
    </script>
  </body>
</html>
```

### `tests/fixtures/browser_site/download.txt`

```text
TaskPilot controlled browser download fixture.
```

## 24. Full Relevant Browser Integration Tests

以下是当前最终测试源码，不是 git diff。

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

        with pytest.raises(BrowserLocatorError, match="count=2"):
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

### `tests/test_browser_executor.py`

```python
"""真实 TaskExecutor/Graph 到真实 Chromium Browser Tools 的离线测试。"""

import json
from pathlib import Path
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from taskpilot.browser import BrowserConfig, BrowserToolProvider
from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.graph import build_graph
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    ToolCallStatus,
    VerificationResult,
)
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry


class FakeFunctionCallingModel:
    """按预设动作驱动真实 Executor，并记录完整 ToolMessage 链。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.bound_tools: list[dict[str, Any]] = []
        self.invocations: list[list[BaseMessage]] = []

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> "FakeFunctionCallingModel":
        self.bound_tools = list(tools)
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations.append(list(messages))
        return self.responses.pop(0)


def _call(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
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


def _finish(
    output: dict[str, Any],
    call_id: str,
    *,
    summary: str = "The local browser step produced verified DOM evidence.",
) -> AIMessage:
    return _call(
        FINISH_STEP_NAME,
        {
            "summary": summary,
            "evidence": ["Text was extracted from the rendered local DOM."],
            "output": output,
        },
        call_id,
    )


def _config(test_name: str) -> BrowserConfig:
    root = Path("tests/.artifacts/browser_executor").resolve() / test_name
    return BrowserConfig(
        headless=True,
        action_timeout_ms=3_000,
        navigation_timeout_ms=5_000,
        download_dir=root / "downloads",
        artifact_dir=root / "screenshots",
    )


def _spec() -> TaskSpec:
    return TaskSpec(
        goal="Use the local page to find two gym candidates",
        constraints={"source": "localhost"},
        completion_criteria=["Return Harbor Gym and Peak Fitness"],
    )


def _step() -> PlanStep:
    return PlanStep(
        id=1,
        description="Open the local page and extract two candidate names",
        success_criteria=["Two rendered candidate names are extracted"],
    )


@pytest.mark.asyncio
async def test_executor_drives_real_browser_and_preserves_tool_message_pairing(
    browser_site_url: str,
) -> None:
    actions = [
        _call(
            "browser_navigate",
            {"url": f"{browser_site_url}/index.html"},
            "browser-1",
        ),
        _call("browser_observe", {}, "browser-2"),
        _call(
            "browser_fill",
            {
                "locator": {"strategy": "label", "value": "Search Query"},
                "value": "gyms",
            },
            "browser-3",
        ),
        _call(
            "browser_click",
            {
                "locator": {
                    "strategy": "role",
                    "role": "button",
                    "name": "Apply filters",
                }
            },
            "browser-4",
        ),
        _call(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#results"}},
            "browser-5",
        ),
        _finish(
            {"candidates": ["Harbor Gym", "Peak Fitness"]},
            "browser-finish",
        ),
    ]
    model = FakeFunctionCallingModel(actions)
    registry = ToolRegistry()

    async with BrowserToolProvider(_config("executor_e2e")) as provider:
        provider.register_tools(registry)
        executor = TaskExecutor(
            registry=registry,
            model=cast(BaseChatModel, model),
            max_actions_per_step=6,
        )
        result = await executor.execute(
            task_spec=_spec(),
            step=_step(),
            task_results={},
            tool_calls=[],
        )
        rendered = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#results"}},
        )

    assert result.fatal_error is False
    assert result.outcome.claimed_complete is True
    assert result.outcome.action_count == 6
    assert result.outcome.output == {
        "candidates": ["Harbor Gym", "Peak Fitness"]
    }
    assert "gyms" in rendered["text"]
    assert "Harbor Gym" in rendered["text"]
    assert [record.tool_name for record in result.tool_calls] == [
        "browser_navigate",
        "browser_observe",
        "browser_fill",
        "browser_click",
        "browser_extract_text",
    ]
    assert all(
        record.status is ToolCallStatus.SUCCESS for record in result.tool_calls
    )
    for index, expected_id in enumerate(
        ["browser-1", "browser-2", "browser-3", "browser-4", "browser-5"],
        start=1,
    ):
        observation = model.invocations[index][-1]
        assert isinstance(observation, ToolMessage)
        assert observation.tool_call_id == expected_id
    print(
        "EXECUTOR_BROWSER_SMOKE="
        + json.dumps(
            {
                "tool_calls": [
                    record.model_dump(mode="json") for record in result.tool_calls
                ],
                "outcome": result.outcome.model_dump(mode="json"),
                "rendered_text": rendered["text"],
            },
            ensure_ascii=False,
        )
    )


@pytest.mark.asyncio
async def test_dom_locator_failure_can_observe_and_recover(
    browser_site_url: str,
) -> None:
    model = FakeFunctionCallingModel(
        [
            _call(
                "browser_navigate",
                {"url": f"{browser_site_url}/index.html"},
                "recover-nav",
            ),
            _call(
                "browser_click",
                {
                    "locator": {
                        "strategy": "role",
                        "role": "button",
                        "name": "Does Not Exist",
                    }
                },
                "recover-wrong",
            ),
            _call("browser_observe", {}, "recover-observe"),
            _call(
                "browser_click",
                {
                    "locator": {
                        "strategy": "role",
                        "role": "button",
                        "name": "Apply filters",
                    }
                },
                "recover-correct",
            ),
            _finish({"recovered": True}, "recover-finish"),
        ]
    )
    registry = ToolRegistry()

    async with BrowserToolProvider(_config("recovery")) as provider:
        provider.register_tools(registry)
        result = await TaskExecutor(
            registry=registry,
            model=cast(BaseChatModel, model),
            max_actions_per_step=5,
        ).execute(
            task_spec=_spec(),
            step=_step(),
            task_results={},
            tool_calls=[],
        )

    assert result.fatal_error is False
    assert result.outcome.claimed_complete is True
    assert [record.status for record in result.tool_calls] == [
        ToolCallStatus.SUCCESS,
        ToolCallStatus.FAILED,
        ToolCallStatus.SUCCESS,
        ToolCallStatus.SUCCESS,
    ]
    assert "count=0" in (result.tool_calls[1].error or "")
    error_observation = model.invocations[2][-1]
    assert isinstance(error_observation, ToolMessage)
    assert error_observation.tool_call_id == "recover-wrong"
    assert error_observation.status == "error"
    print(
        "DOM_RECOVERY_SMOKE="
        + json.dumps(
            {
                "tool_calls": [
                    record.model_dump(mode="json") for record in result.tool_calls
                ],
                "fatal_error": result.fatal_error,
                "outcome": result.outcome.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
    )


class _FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(
            goal=user_input,
            constraints={"source": "localhost"},
            completion_criteria=["Return two candidate names"],
        )


class _FakePlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(steps=[_step()])


class _FakeVerifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        step = kwargs["step"]
        return StepVerificationResult(
            step_id=step.id,
            verified=True,
            checks=[
                CriterionCheck(
                    criterion_index=0,
                    satisfied=True,
                    reason="The real browser extraction returned two names.",
                )
            ],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(
            completed=True,
            reason="The verified StepResult contains both candidate names.",
        )


def _initial_state() -> TaskState:
    return {
        "task_id": "phase-7-browser-graph",
        "user_input": "打开本地页面并提取两个候选名称",
        "task_spec": None,
        "plan": [],
        "current_step_id": None,
        "step_outcome": None,
        "step_results": [],
        "step_verification": None,
        "tool_calls": [],
        "task_results": {},
        "verification": None,
        "status": TaskStatus.CREATED,
        "error": None,
    }


@pytest.mark.asyncio
async def test_full_graph_completes_with_real_browser_executor(
    browser_site_url: str,
) -> None:
    model = FakeFunctionCallingModel(
        [
            _call(
                "browser_navigate",
                {"url": f"{browser_site_url}/details.html"},
                "graph-nav",
            ),
            _call("browser_observe", {}, "graph-observe"),
            _call(
                "browser_extract_text",
                {},
                "graph-extract",
            ),
            _finish(
                {"candidates": ["Harbor Gym", "Peak Fitness"]},
                "graph-finish",
            ),
        ]
    )
    registry = ToolRegistry()

    async with BrowserToolProvider(_config("full_graph")) as provider:
        provider.register_tools(registry)
        executor = TaskExecutor(
            registry=registry,
            model=cast(BaseChatModel, model),
            max_actions_per_step=4,
        )
        result = await build_graph(
            analyzer=_FakeAnalyzer(),
            planner=_FakePlanner(),
            executor=executor,
            verifier=_FakeVerifier(),
        ).ainvoke(_initial_state())

    assert result["status"] is TaskStatus.COMPLETED
    assert result["error"] is None
    assert result["plan"][0].status.value == "completed"
    assert [record.tool_name for record in result["tool_calls"]] == [
        "browser_navigate",
        "browser_observe",
        "browser_extract_text",
    ]
    assert result["step_results"][0].output == {
        "candidates": ["Harbor Gym", "Peak Fitness"]
    }
    assert result["verification"].completed is True
    print(
        "FULL_GRAPH_BROWSER_SMOKE="
        + json.dumps(
            {
                "plan": [step.model_dump(mode="json") for step in result["plan"]],
                "tool_calls": [
                    record.model_dump(mode="json")
                    for record in result["tool_calls"]
                ],
                "step_results": [
                    item.model_dump(mode="json")
                    for item in result["step_results"]
                ],
                "status": result["status"].value,
                "error": result["error"],
            },
            ensure_ascii=False,
        )
    )
```

## 25. Pytest Actual Result

执行命令：

```powershell
conda run -n ai python -m pytest -q
```

真实输出：

```text
........................................................................ [ 75%]
.......................                                                  [100%]
95 passed in 7.36s
```

结果：95 passed，0 failed。Phase 1–6 测试（含真实 STDIO MCP integration）与 Phase 7 新测试共同通过。

## 26. Real Chromium Navigation Smoke

命令：

```powershell
conda run -n ai python -m pytest tests/test_browser.py::test_real_browser_tools_cover_dom_pages_iframe_download_and_cleanup -q -s
```

真实输出：

```text
BROWSER_SMOKE={"browser_version": "151.0.7922.34", "navigation": {"page_id": "page-1", "final_url": "http://127.0.0.1:7322/index.html", "title": "TaskPilot Browser Fixture", "status": 200}, "semantic_result": {"page_id": "page-1", "text": "Results for Hong Kong gym; category=gyms; inactive=true: Harbor Gym, Peak Fitness", "truncated": false, "original_length": 81}, "iframe_result": {"page_id": "page-1", "text": "Frame value: inside iframe", "truncated": false, "original_length": 26}, "popup": {"clicked": true, "page_id": "page-1", "current_url": "http://127.0.0.1:7322/index.html", "new_page_id": "page-2"}, "download": {"filename": "report_5.txt", "saved_path": "D:\\Desktop\\TaskPilot\\tests\\.artifacts\\browser\\downloads\\report_5.txt", "suggested_filename": "report.txt", "page_id": "page-1"}, "screenshot": {"saved_path": "D:\\Desktop\\TaskPilot\\tests\\.artifacts\\browser\\screenshots\\screenshot_0001_5.png", "page_id": "page-1", "url": "http://127.0.0.1:7322/index.html"}, "remaining_pages": [{"page_id": "page-1", "active": true, "title": "TaskPilot Browser Fixture", "url": "http://127.0.0.1:7322/index.html"}]}
.
1 passed in 1.76s
```

其中 navigation 返回 HTTP 200、真实 title 和 final localhost URL；browser version 在第 3/4 节独立真实启动命令中记录。

## 27. Semantic Locator Form Smoke

同一真实 smoke 依次用 label Locator fill/select/check，再用 role+name 点击 `Apply filters`。DOM 提取实际得到：

```text
Results for Hong Kong gym; category=gyms; inactive=true: Harbor Gym, Peak Fitness
```

CSS 仅另行验证 `#css-fallback` 和提取目标可以工作；主要交互路径使用语义 Locator。两个同名按钮匹配时明确报 `count=2`。

## 28. iframe Smoke

真实结果：

```json
{"text": "Frame value: inside iframe", "truncated": false, "original_length": 26}
```

填值、点击和提取均通过 `frame_chain + FrameLocator` 完成。

## 29. Popup / Tab Smoke

真实点击结果包含 `"new_page_id": "page-2"`。随后 list pages 得到 page-1/page-2，switch page-2 后 title 为 `TaskPilot Details`。关闭 popup 后映射只剩 active page-1。

## 30. Download Smoke

真实输出记录 `suggested_filename: report.txt`，保存路径位于 resolved test `download_dir`。文件内容为：

```text
TaskPilot controlled browser download fixture.
```

重复执行产生受控的 `report_N.txt`，不会覆盖旧文件。单元测试同时验证 `../danger report.txt` 被降为 basename 并 sanitize。

## 31. Executor → Browser End-to-End Smoke

动作链：

```text
browser_navigate → browser_observe → browser_fill
→ browser_click → browser_extract_text → finish_step
```

真实命令与输出（同一 localhost server，真实 TaskExecutor、Registry、BrowserSession、Chromium）：

```powershell
conda run -n ai python -m pytest tests/test_browser_executor.py -q -s
```

```text
EXECUTOR_BROWSER_SMOKE={"tool_calls": [{"call_id": "browser-1", "step_id": 1, "tool_name": "browser_navigate", "arguments": {"url": "http://127.0.0.1:6434/index.html"}, "status": "success", "result": {"page_id": "page-1", "final_url": "http://127.0.0.1:6434/index.html", "title": "TaskPilot Browser Fixture", "status": 200}, "error": null}, {"call_id": "browser-2", "step_id": 1, "tool_name": "browser_observe", "arguments": {}, "status": "success", "result": {"page_id": "page-1", "url": "http://127.0.0.1:6434/index.html", "title": "TaskPilot Browser Fixture", "aria_snapshot": "- main [ref=e2]:\n  - heading \"TaskPilot Browser Fixture\" [level=1] [ref=e3]\n  - text: Search Query\n  - textbox \"Search Query\" [ref=e4]:\n    - /placeholder: Enter query\n  - text: Category\n  - combobox \"Category\" [ref=e5]:\n    - option \"Gyms\" [selected]\n    - option \"Cafes\"\n  - generic [ref=e6]:\n    - checkbox \"Include inactive\" [ref=e7]\n    - text: Include inactive\n  - button \"Apply filters\" [ref=e8]\n  - generic [ref=e9]: No results yet.\n  - paragraph [ref=e10]: CSS fallback target\n  - link \"Open details\" [ref=e11] [cursor=pointer]:\n    - /url: /details.html\n  - link \"Open details popup\" [ref=e12] [cursor=pointer]:\n    - /url: /details.html\n  - link \"Download report\" [ref=e13] [cursor=pointer]:\n    - /url: /download.txt\n  - iframe [ref=e14]:\n    - generic [ref=f1e1]:\n      - text: Frame Input\n      - textbox \"Frame Input\" [ref=f1e2]\n      - button \"Update frame\" [ref=f1e3]\n      - paragraph [ref=f1e4]: Frame waiting.\n  - button \"Duplicate action\" [ref=e15]\n  - button \"Duplicate action\" [ref=e16]", "snapshot_mode": "ai", "depth": 8, "truncated": false, "original_length": 1009, "pages": [{"page_id": "page-1", "active": true, "title": "TaskPilot Browser Fixture", "url": "http://127.0.0.1:6434/index.html"}]}, "error": null}, {"call_id": "browser-3", "step_id": 1, "tool_name": "browser_fill", "arguments": {"locator": {"strategy": "label", "value": "Search Query"}, "value": "gyms"}, "status": "success", "result": {"filled": true, "page_id": "page-1", "value": "gyms"}, "error": null}, {"call_id": "browser-4", "step_id": 1, "tool_name": "browser_click", "arguments": {"locator": {"strategy": "role", "role": "button", "name": "Apply filters"}}, "status": "success", "result": {"clicked": true, "page_id": "page-1", "current_url": "http://127.0.0.1:6434/index.html", "new_page_id": null}, "error": null}, {"call_id": "browser-5", "step_id": 1, "tool_name": "browser_extract_text", "arguments": {"locator": {"strategy": "css", "value": "#results"}}, "status": "success", "result": {"page_id": "page-1", "text": "Results for gyms; category=gyms; inactive=false: Harbor Gym, Peak Fitness", "truncated": false, "original_length": 73}, "error": null}], "outcome": {"step_id": 1, "claimed_complete": true, "summary": "The local browser step produced verified DOM evidence.", "evidence": ["Text was extracted from the rendered local DOM."], "output": {"candidates": ["Harbor Gym", "Peak Fitness"]}, "action_count": 6, "error": null}, "rendered_text": "Results for gyms; category=gyms; inactive=false: Harbor Gym, Peak Fitness"}
.DOM_RECOVERY_SMOKE={"tool_calls": [{"call_id": "recover-nav", "step_id": 1, "tool_name": "browser_navigate", "arguments": {"url": "http://127.0.0.1:6434/index.html"}, "status": "success", "result": {"page_id": "page-1", "final_url": "http://127.0.0.1:6434/index.html", "title": "TaskPilot Browser Fixture", "status": 200}, "error": null}, {"call_id": "recover-wrong", "step_id": 1, "tool_name": "browser_click", "arguments": {"locator": {"strategy": "role", "role": "button", "name": "Does Not Exist"}}, "status": "failed", "result": null, "error": "BrowserLocatorError: browser_click locator must match exactly one element; page_url=http://127.0.0.1:6434/index.html; strategy=role; count=0"}, {"call_id": "recover-observe", "step_id": 1, "tool_name": "browser_observe", "arguments": {}, "status": "success", "result": {"page_id": "page-1", "url": "http://127.0.0.1:6434/index.html", "title": "TaskPilot Browser Fixture", "aria_snapshot": "- main [ref=e2]:\n  - heading \"TaskPilot Browser Fixture\" [level=1] [ref=e3]\n  - text: Search Query\n  - textbox \"Search Query\" [ref=e4]:\n    - /placeholder: Enter query\n  - text: Category\n  - combobox \"Category\" [ref=e5]:\n    - option \"Gyms\" [selected]\n    - option \"Cafes\"\n  - generic [ref=e6]:\n    - checkbox \"Include inactive\" [ref=e7]\n    - text: Include inactive\n  - button \"Apply filters\" [ref=e8]\n  - generic [ref=e9]: No results yet.\n  - paragraph [ref=e10]: CSS fallback target\n  - link \"Open details\" [ref=e11] [cursor=pointer]:\n    - /url: /details.html\n  - link \"Open details popup\" [ref=e12] [cursor=pointer]:\n    - /url: /details.html\n  - link \"Download report\" [ref=e13] [cursor=pointer]:\n    - /url: /download.txt\n  - iframe [ref=e14]:\n    - generic [ref=f1e1]:\n      - text: Frame Input\n      - textbox \"Frame Input\" [ref=f1e2]\n      - button \"Update frame\" [ref=f1e3]\n      - paragraph [ref=f1e4]: Frame waiting.\n  - button \"Duplicate action\" [ref=e15]\n  - button \"Duplicate action\" [ref=e16]", "snapshot_mode": "ai", "depth": 8, "truncated": false, "original_length": 1009, "pages": [{"page_id": "page-1", "active": true, "title": "TaskPilot Browser Fixture", "url": "http://127.0.0.1:6434/index.html"}]}, "error": null}, {"call_id": "recover-correct", "step_id": 1, "tool_name": "browser_click", "arguments": {"locator": {"strategy": "role", "role": "button", "name": "Apply filters"}}, "status": "success", "result": {"clicked": true, "page_id": "page-1", "current_url": "http://127.0.0.1:6434/index.html", "new_page_id": null}, "error": null}], "fatal_error": false, "outcome": {"step_id": 1, "claimed_complete": true, "summary": "The local browser step produced verified DOM evidence.", "evidence": ["Text was extracted from the rendered local DOM."], "output": {"recovered": true}, "action_count": 5, "error": null}}
.FULL_GRAPH_BROWSER_SMOKE={"plan": [{"id": 1, "description": "Open the local page and extract two candidate names", "status": "completed", "retry_count": 0, "depends_on": [], "success_criteria": ["Two rendered candidate names are extracted"]}], "tool_calls": [{"call_id": "graph-nav", "step_id": 1, "tool_name": "browser_navigate", "arguments": {"url": "http://127.0.0.1:6434/details.html"}, "status": "success", "result": {"page_id": "page-1", "final_url": "http://127.0.0.1:6434/details.html", "title": "TaskPilot Details", "status": 200}, "error": null}, {"call_id": "graph-observe", "step_id": 1, "tool_name": "browser_observe", "arguments": {}, "status": "success", "result": {"page_id": "page-1", "url": "http://127.0.0.1:6434/details.html", "title": "TaskPilot Details", "aria_snapshot": "- main [ref=e2]:\n  - heading \"Details Page\" [level=1] [ref=e3]\n  - paragraph [ref=e4]: Harbor Gym costs HKD 420 per month.\n  - paragraph [ref=e5]: Peak Fitness costs HKD 480 per month.", "snapshot_mode": "ai", "depth": 8, "truncated": false, "original_length": 184, "pages": [{"page_id": "page-1", "active": true, "title": "TaskPilot Details", "url": "http://127.0.0.1:6434/details.html"}]}, "error": null}, {"call_id": "graph-extract", "step_id": 1, "tool_name": "browser_extract_text", "arguments": {}, "status": "success", "result": {"page_id": "page-1", "text": "Details Page\n\nHarbor Gym costs HKD 420 per month.\n\nPeak Fitness costs HKD 480 per month.", "truncated": false, "original_length": 88}, "error": null}], "step_results": [{"step_id": 1, "summary": "The local browser step produced verified DOM evidence.", "output": {"candidates": ["Harbor Gym", "Peak Fitness"]}, "evidence": ["Text was extracted from the rendered local DOM."]}], "status": "completed", "error": null}
.
3 passed in 2.60s
```

五个外部动作均形成 browser ToolCallRecord，最后内部 `finish_step` 产生 `StepExecutionOutcome(action_count=6)`。每一轮下一次模型 invocation 的末尾 ToolMessage ID 与前一 Tool Call ID 一一对应。

## 32. DOM Failure → Observe → Recover Smoke

真实顺序：

```text
navigate success
→ click(role=button, name="Does Not Exist") failed (count=0)
→ browser_observe success
→ click(role=button, name="Apply filters") success
→ finish_step
```

记录中错误 Locator 是 FAILED ToolCallRecord，`fatal_error=false`，最终 Outcome 仍 `claimed_complete=true`。这证明普通 DOM failure 不会自动把整个 Task 判为 fatal。

## 33. Full Graph Browser Smoke

实际链路：

```text
Fake Analyzer → Fake Planner → real TaskExecutor
→ real Playwright Browser Tools → finish_step
→ Fake Step Verifier → advance_step → Fake Task Verifier → COMPLETED
```

完整输出位于第 31 节的 `FULL_GRAPH_BROWSER_SMOKE`。关键最终值：

```json
{
  "plan_status": "completed",
  "browser_tool_calls": [
    "browser_navigate",
    "browser_observe",
    "browser_extract_text"
  ],
  "step_result_output": {
    "candidates": ["Harbor Gym", "Peak Fitness"]
  },
  "task_status": "completed",
  "error": null
}
```

Analyzer/Planner/Verifier 是 deterministic Fake；Executor、Registry、Browser Provider、Chromium 和 DOM 操作都是真实实现。

## 34. Browser + MCP Coexistence

`test_browser_mcp_and_local_tools_share_one_registry` 在同一个 Registry 注册：

- `local_echo` Local Tool；
- `demo__lookup` MCPToolAdapter；
- 11 个 `browser_*` Tool。

Function schema names 无冲突，Local 与 MCP adapter 都能经同一 `registry.invoke` 调用。Browser 没有分叉另一套 Runtime。

## 35. Known Limitations

- Browser 只支持 Chromium。
- AI snapshot refs 只用于观察，不直接作为 action selector。
- 没有任意 JavaScript Tool，也没有 XPath。
- Locator failure 的恢复依赖 Executor 下一轮模型选择；没有 RecoveryManager。
- 没有登录凭据策略、持久化 profile、跨 Task cookie/session 恢复。
- 没有高风险外部写操作审批、HITL 或 RiskPolicy。
- screenshot 不接 Vision/OCR，DOM failure 不会自动转坐标点击。
- 没有自动重连、checkpoint 或 dynamic replanning。
- Browser 结果有简单字符上限，但没有完整 context compression。
- 下载目录保护只覆盖 Browser 生成文件，不是生产 Filesystem sandbox。
- 本阶段没有真实生产网站测试，也没有 Benchmark 数据。

## 36. Files Changed

Phase 7 新建：

- `src/taskpilot/browser/__init__.py`：公开 Browser API；
- `src/taskpilot/browser/config.py`：轻量、安全目录配置；
- `src/taskpilot/browser/session.py`：Playwright async lifecycle/page IDs；
- `src/taskpilot/browser/locators.py`：语义 LocatorSpec/JSON Schema；
- `src/taskpilot/browser/tools.py`：11 个 Tools 与 Provider；
- `tests/conftest.py`：本地 HTTP server fixture；
- `tests/fixtures/browser_site/*`：确定性 HTML/download fixture；
- `tests/test_browser.py`：真实 Chromium Browser integration；
- `tests/test_browser_executor.py`：真实 Executor/Graph/recovery smoke；
- `docs/review/phase_07.md`：本验收文档。

Phase 7 修改：

- `pyproject.toml`：增加 `playwright>=1.62,<2`；
- `.gitignore`：忽略 Browser 产物，并允许跟踪 test HTML fixture；
- `src/taskpilot/cli.py`：AsyncExitStack、`--browser`、`--headed`；
- `README.md`：Phase 7 能力、运行方式和高风险边界。

Phase 7 没有为 Browser 改写 Graph、Executor、MCP 架构或 TaskState。

## 37. Git Status

该节在文档生成后用最终 `git status --short` 实际输出更新。工作区包含此前 Phase 的未提交修改，均保留，未删除或回滚。

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
?? src/taskpilot/browser/
?? src/taskpilot/executor.py
?? src/taskpilot/mcp/
?? src/taskpilot/tools/
?? src/taskpilot/verifier.py
?? tests/conftest.py
?? tests/fixtures/
?? tests/test_browser.py
?? tests/test_browser_executor.py
?? tests/test_executor.py
?? tests/test_mcp.py
?? tests/test_mcp_integration.py
?? tests/test_phase5_graph.py
?? tests/test_tools.py
?? tests/test_verifier.py
```

## 38. Boundary Checks (Q1–Q12)

### Q1：Browser 是否重新启动于每个 Tool Call？

**No。** 一个 `BrowserToolProvider` async context 对应一个长生命周期 `BrowserSession`；所有 Tool 共用同一 BrowserContext/pages/cookies/form state。

### Q2：Browser 是否是 LangGraph Node infrastructure？

**No。** Browser 由 Host 在 Graph 外启动并注册 Tool；Graph node 列表不含 connect/launch/observe browser infrastructure node。

### Q3：默认 Locator 是否依赖 CSS nth-child？

**No。** 描述优先 role/label/test_id/text/placeholder；CSS 仅 fallback，Runtime 不生成 nth-child。

### Q4：DOM interaction 是否使用 Playwright Locator + actionability？

**Yes。** 使用公开 Locator 的 `click/fill/select_option/check/inner_text`，保留 strictness、auto-wait 和 actionability，不使用 `force=True`。

### Q5：Browser Tool Error 是否立刻导致整个 Task fatal？

**No（普通环境错误）。** Locator/navigation 等 Tool exception 被 Executor 转成 FAILED ToolCallRecord + error ToolMessage，模型可继续 observe/换 locator。未知工具、schema 错误、模型协议错误等 Executor contract failure 仍可 fatal。

### Q6：LLM 是否能执行任意 JavaScript？

**No。** Registry 中没有 `browser_evaluate` 或同类工具，导航还拒绝 `javascript:` 与 `data:` scheme。

### Q7：Browser 下载能否写任意系统路径？

**No。** Tool schema 不接收路径；只使用 sanitized suggested filename 写入 resolved `BrowserConfig.download_dir`，并再次检查 parent。

### Q8：是否真的跑过 Chromium，而不是 Mock Page？

**Yes。** 第 3/4/25/26/31 节记录了真实启动的 Chromium `151.0.7922.34`、安装/测试命令、HTTP 200、DOM 和文件输出。Browser integration tests 没有 Mock Page。

### Q9：iframe 是否真实交互？

**Yes。** 本地 index 真嵌入 `frame.html`，测试通过 FrameLocator 填值、点击并提取到 `Frame value: inside iframe`。

### Q10：popup 是否拥有稳定 page_id？

**Yes。** 官方 popup event 返回真实 Page，由 Session 注册为 `page-2`，可 list/switch/observe；关闭后映射清理。

### Q11：当前是否已经实现 Vision fallback？

**No。** screenshot 只落受控文件，没有 Vision Model、OCR 或 coordinate click。

### Q12：Browser、MCP、Local Tools 是否共用同一个 ToolRegistry？

**Yes。** coexistence test 在同一个 Registry 同时注册并验证三类 Tool schema/invoke；CLI 也用同一 Registry 组合 Provider。

## 39. CLI / Host Source

CLI 最终完整源码如下。没有 MCP/Browser 时仍使用空 Registry 并诚实失败；启用任一 Provider 后，它们覆盖整个 `graph.ainvoke` 生命周期。

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

from taskpilot.browser import BrowserConfig, BrowserToolProvider
from taskpilot.executor import TaskExecutor
from taskpilot.graph import build_graph
from taskpilot.mcp.config import load_mcp_server_configs
from taskpilot.mcp.provider import MCPToolProvider
from taskpilot.models import TaskStatus
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

        executor = TaskExecutor(registry=registry)
        result = await build_graph(executor=executor).ainvoke(
            create_initial_state(args.task)
        )
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
    step_outcome = result["step_outcome"]
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
    verification = result["verification"]
    if verification is not None:
        print(f"task_verified={verification.completed}")
        print(f"verification_reason={verification.reason}")
    if result["error"]:
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

CLI flag smoke：

```powershell
cd src
conda run -n ai python -m taskpilot.cli --help
```

```text
usage: cli.py [-h] [--mcp-config MCP_CONFIG] [--browser] [--headed] task

Run the TaskPilot graph.
```
