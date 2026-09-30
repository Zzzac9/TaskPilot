"""MCP config、namespace、归一化和 transport lifecycle 的离线测试。"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, cast

import pytest
from mcp import ClientSession
from mcp.types import (
    CallToolResult,
    ImageContent,
    ListToolsResult,
    TextContent,
    Tool as RemoteTool,
)
from pydantic import ValidationError

import taskpilot.mcp.provider as provider_module
from taskpilot.mcp import (
    MCPConnectionError,
    MCPConfigurationError,
    MCPServerConfig,
    MCPToolAdapter,
    MCPToolExecutionError,
    MCPToolProvider,
    build_public_tool_name,
    normalize_call_tool_result,
)
from taskpilot.tools import ToolRegistry


def test_stdio_and_streamable_http_config_validation() -> None:
    stdio = MCPServerConfig(
        server_id="demo",
        transport="stdio",
        command="python",
        args=["server.py"],
    )
    http = MCPServerConfig(
        server_id="remote",
        transport="streamable_http",
        url="http://127.0.0.1:8765/mcp",
    )

    assert stdio.command == "python"
    assert http.url == "http://127.0.0.1:8765/mcp"
    assert stdio.trust_tool_annotations is False

    with pytest.raises(ValidationError, match="必须提供 command"):
        MCPServerConfig(server_id="bad", transport="stdio")
    with pytest.raises(ValidationError, match=r"必须提供 http\(s\) url"):
        MCPServerConfig(server_id="bad", transport="streamable_http")
    with pytest.raises(ValidationError, match="不能提供 command"):
        MCPServerConfig(
            server_id="bad",
            transport="streamable_http",
            url="https://example.invalid/mcp",
            command="python",
        )


def test_stdio_env_is_explicit_and_secret_in_dumps() -> None:
    config = MCPServerConfig(
        server_id="demo",
        transport="stdio",
        command="python",
        env={"DEMO_TOKEN": "phase-6-secret"},
    )

    assert config.resolved_env() == {"DEMO_TOKEN": "phase-6-secret"}
    assert "phase-6-secret" not in repr(config)
    assert "phase-6-secret" not in str(config.model_dump(mode="json"))
    assert "LLM_API_KEY" not in config.resolved_env()


def test_public_tool_name_is_namespaced_safe_and_deterministic() -> None:
    first = build_public_tool_name("web server", "search.tools/v1")
    second = build_public_tool_name("web server", "search.tools/v1")
    long_name = build_public_tool_name("server" * 20, "tool" * 30)

    assert first == "web_server__search_tools_v1"
    assert second == first
    assert build_public_tool_name("web-a", "search") != (
        build_public_tool_name("web-b", "search")
    )
    assert len(long_name) == 64
    assert all(character.isalnum() or character in "_-" for character in first)


def test_provider_rejects_duplicate_and_sanitized_server_ids() -> None:
    duplicate = [
        MCPServerConfig(server_id="web", transport="stdio", command="python"),
        MCPServerConfig(server_id="web", transport="stdio", command="python"),
    ]
    collision = [
        MCPServerConfig(server_id="web.one", transport="stdio", command="python"),
        MCPServerConfig(server_id="web_one", transport="stdio", command="python"),
    ]

    with pytest.raises(MCPConfigurationError, match="server_id 必须唯一"):
        MCPToolProvider(duplicate)
    with pytest.raises(MCPConfigurationError, match="namespace 冲突"):
        MCPToolProvider(collision)


def test_result_normalization_preserves_multiple_content_types() -> None:
    result = CallToolResult(
        structuredContent={"answer": 3},
        content=[
            TextContent(text="three"),
            ImageContent(data="aW1hZ2U=", mimeType="image/png"),
        ],
    )

    normalized = normalize_call_tool_result(result)

    assert normalized["structured_content"] == {"answer": 3}
    assert normalized["content"][0] == {
        "type": "text",
        "text": "three",
        "annotations": None,
        "meta": None,
    }
    assert normalized["content"][1]["type"] == "image"
    assert normalized["content"][1]["mime_type"] == "image/png"
    assert normalized["is_error"] is False


class FakeCallSession:
    def __init__(self, result: CallToolResult) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        self.calls.append((name, arguments))
        return self.result


@pytest.mark.asyncio
async def test_adapter_calls_remote_name_not_public_namespace() -> None:
    session = FakeCallSession(
        CallToolResult(
            content=[TextContent(text="hello")],
            structuredContent={"echoed": "hello"},
        )
    )
    adapter = MCPToolAdapter(
        name="demo__echo",
        remote_tool_name="echo",
        server_id="demo",
        description="Echo",
        input_schema={"type": "object"},
        session=cast(ClientSession, session),
    )

    result = await adapter.invoke({"text": "hello"})

    assert session.calls == [("echo", {"text": "hello"})]
    assert result["structured_content"] == {"echoed": "hello"}


@pytest.mark.asyncio
async def test_adapter_raises_for_mcp_is_error_result() -> None:
    session = FakeCallSession(
        CallToolResult(
            content=[TextContent(text="remote failure detail")],
            isError=True,
        )
    )
    adapter = MCPToolAdapter(
        name="demo__fail",
        remote_tool_name="fail",
        server_id="demo",
        description="Fail",
        input_schema={"type": "object"},
        session=cast(ClientSession, session),
    )

    with pytest.raises(MCPToolExecutionError, match="is_error=true") as error:
        await adapter.invoke({})

    assert error.value.result["is_error"] is True
    assert "remote failure detail" in str(error.value)


class FakePaginatedSession:
    def __init__(self) -> None:
        self.cursors: list[str | None] = []

    async def list_tools(self, *, params=None) -> ListToolsResult:
        cursor = None if params is None else params.cursor
        self.cursors.append(cursor)
        if cursor is None:
            return ListToolsResult(
                tools=[
                    RemoteTool(
                        name="first",
                        description="First",
                        inputSchema={"type": "object"},
                    )
                ],
                nextCursor="page-2",
            )
        return ListToolsResult(
            tools=[
                RemoteTool(
                    name="second",
                    description="Second",
                    inputSchema={"type": "object"},
                )
            ]
        )


@pytest.mark.asyncio
async def test_list_tools_follows_all_pagination_cursors() -> None:
    session = FakePaginatedSession()

    tools = await MCPToolProvider._list_all_tools(cast(ClientSession, session))

    assert [tool.name for tool in tools] == ["first", "second"]
    assert session.cursors == [None, "page-2"]


class LocalAsyncTool:
    name = "local_calculator"
    description = "Local async calculator"
    input_schema = {"type": "object", "additionalProperties": False}

    async def invoke(self, arguments: dict[str, Any]) -> Any:
        return {"local": True}


def test_local_and_mcp_tools_coexist_in_function_schemas() -> None:
    adapter = MCPToolAdapter(
        name="demo__echo",
        remote_tool_name="echo",
        server_id="demo",
        description="MCP echo",
        input_schema={"type": "object"},
        session=cast(ClientSession, FakeCallSession(CallToolResult(content=[]))),
    )
    registry = ToolRegistry()
    registry.register(LocalAsyncTool())
    registry.register(adapter)

    names = [
        schema["function"]["name"]
        for schema in registry.list_function_schemas()
    ]

    assert names == ["local_calculator", "demo__echo"]
    print("LOCAL_MCP_COEXISTENCE=" + ",".join(names))


@pytest.mark.asyncio
async def test_connection_error_redacts_explicit_stdio_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def failing_transport(parameters) -> AsyncIterator[tuple[str, str]]:
        raise RuntimeError(f"spawn rejected token={parameters.env['TOKEN']}")
        yield ("unreachable", "unreachable")  # pragma: no cover

    monkeypatch.setattr(provider_module, "stdio_client", failing_transport)
    config = MCPServerConfig(
        server_id="broken",
        transport="stdio",
        command="missing",
        env={"TOKEN": "private-mcp-token"},
    )

    with pytest.raises(MCPConnectionError) as error:
        async with MCPToolProvider([config]):
            pass

    assert "private-mcp-token" not in str(error.value)
    assert "[REDACTED]" in str(error.value)


class FakeHTTPClientSession:
    instances: list["FakeHTTPClientSession"] = []

    def __init__(self, *streams: Any) -> None:
        self.streams = streams
        self.initialized = False
        self.closed = False
        self.__class__.instances.append(self)

    async def __aenter__(self) -> "FakeHTTPClientSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.closed = True

    async def initialize(self) -> None:
        self.initialized = True


@pytest.mark.asyncio
async def test_streamable_http_transport_lifecycle_is_constructed_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_urls: list[str] = []
    transport_closed = False

    @asynccontextmanager
    async def fake_http_transport(url: str) -> AsyncIterator[tuple[str, str]]:
        nonlocal transport_closed
        opened_urls.append(url)
        try:
            yield ("read-stream", "write-stream")
        finally:
            transport_closed = True

    FakeHTTPClientSession.instances.clear()
    monkeypatch.setattr(
        provider_module,
        "streamable_http_client",
        fake_http_transport,
    )
    monkeypatch.setattr(provider_module, "ClientSession", FakeHTTPClientSession)
    config = MCPServerConfig(
        server_id="http-demo",
        transport="streamable_http",
        url="http://127.0.0.1:8765/mcp",
    )

    async with MCPToolProvider([config]) as provider:
        assert provider.connected_server_ids == ["http-demo"]
        session = FakeHTTPClientSession.instances[0]
        assert session.streams == ("read-stream", "write-stream")
        assert session.initialized is True
        assert session.closed is False

    assert opened_urls == ["http://127.0.0.1:8765/mcp"]
    assert session.closed is True
    assert transport_closed is True
    assert provider.connected_server_ids == []


def test_mcp_config_file_loader() -> None:
    config_path = Path(__file__).parent / "fixtures" / "mcp_config.json"

    from taskpilot.mcp.config import load_mcp_server_configs

    configs = load_mcp_server_configs(config_path)

    assert len(configs) == 1
    assert configs[0].server_id == "demo"
