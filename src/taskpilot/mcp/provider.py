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

        annotations = (
            remote_tool.annotations.model_dump(mode="json", by_alias=False)
            if remote_tool.annotations is not None
            else None
        )
        # 只有 Host 信任 annotations 时，远端 read-only/idempotent hint 才能
        # 降低重放风险；不可信或缺失 annotation 一律保守为 False。
        replay_safe = bool(
            config.trust_tool_annotations
            and isinstance(annotations, dict)
            and (
                annotations.get("read_only_hint") is True
                or annotations.get("idempotent_hint") is True
            )
        )
        metadata = {
            "source": "mcp",
            "trust_tool_annotations": config.trust_tool_annotations,
            "replay_safe": replay_safe,
            "title": remote_tool.title,
            "annotations": annotations,
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
