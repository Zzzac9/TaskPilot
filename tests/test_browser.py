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
async def test_browser_runtime_recreation_reobserves_environment_not_live_dom(
    browser_site_url: str,
) -> None:
    """新 BrowserSession 可工作，但不会伪装恢复旧 Page/DOM 内存。"""

    first_registry = ToolRegistry()
    async with BrowserToolProvider(_browser_config()) as first_provider:
        first_provider.register_tools(first_registry)
        await first_registry.invoke(
            "browser_navigate", {"url": f"{browser_site_url}/index.html"}
        )
        first_observation = await first_registry.invoke("browser_observe", {})
        assert first_observation["title"] == "TaskPilot Browser Fixture"

    rebuilt_registry = ToolRegistry()
    async with BrowserToolProvider(_browser_config()) as rebuilt_provider:
        rebuilt_provider.register_tools(rebuilt_registry)
        rebuilt_observation = await rebuilt_registry.invoke("browser_observe", {})

    assert rebuilt_observation["page_id"] == "page-1"
    assert rebuilt_observation["url"] != first_observation["url"]
    assert "TaskPilot Browser Fixture" not in rebuilt_observation["title"]


@pytest.mark.asyncio
async def test_real_browser_tools_cover_dom_pages_iframe_download_and_cleanup(
    browser_site_url: str,
) -> None:
    registry = ToolRegistry()
    provider = BrowserToolProvider(_browser_config())

    async with provider:
        tools = provider.register_tools(registry)
        assert len(tools) == 12
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

        pressed = await registry.invoke(
            "browser_press",
            {
                "locator": {"strategy": "label", "value": "Search Query"},
                "key": "Enter",
            },
        )
        assert pressed["pressed"] is True
        assert pressed["key"] == "Enter"
        keyboard_results = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#results"}},
        )
        assert "Keyboard results for Hong Kong gym" in keyboard_results["text"]

        css_fallback = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#css-fallback"}},
        )
        assert css_fallback["text"] == "CSS fallback target"

        active_page_text = await registry.invoke(
            "browser_extract_text",
            {
                "page_id": "",
                "locator": {"strategy": "css", "value": "#css-fallback"},
            },
        )
        assert active_page_text["page_id"] == "page-1"
        assert active_page_text["text"] == "CSS fallback target"

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

        indexed_duplicate = await registry.invoke(
            "browser_click",
            {
                "locator": {
                    "strategy": "role",
                    "role": "button",
                    "name": "Duplicate action",
                    "index": 0,
                }
            },
        )
        assert indexed_duplicate["clicked"] is True
        assert indexed_duplicate["active_page_id"] == "page-1"
        assert indexed_duplicate["pages"][0]["page_id"] == "page-1"

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
