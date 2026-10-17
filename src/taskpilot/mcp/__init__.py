"""官方 MCP SDK 到 TaskPilot Tool Runtime 的基础设施适配层。

本包对外只暴露下面 `__all__` 中的公共符号，作为整个 MCP 子系统的统一入口。
调用方应从这里导入，而不是直接依赖各子模块的内部实现。
"""

# adapter：把外部 MCP 工具包装成 TaskPilot 可执行工具，并统一结果/错误的规范化。
from taskpilot.mcp.adapter import (
    MCPToolAdapter,
    MCPToolExecutionError,
    MCPTransportError,
    build_public_tool_name,
    normalize_call_tool_result,
)
# config：定义 MCP 服务器配置的数据结构、传输类型枚举，以及配置加载入口。
from taskpilot.mcp.config import (
    MCPServerConfig,
    MCPTransport,
    load_mcp_server_configs,
)
# provider：负责连接并管理外部 MCP 服务器，暴露连接/配置相关的异常类型。
from taskpilot.mcp.provider import (
    MCPConnectionError,
    MCPConfigurationError,
    MCPToolProvider,
)

# 显式声明本包的公共 API 面：`from taskpilot.mcp import *` 只导出下面这些符号。
__all__ = [
    "MCPConfigurationError",
    "MCPConnectionError",
    "MCPServerConfig",
    "MCPToolAdapter",
    "MCPToolExecutionError",
    "MCPToolProvider",
    "MCPTransport",
    "MCPTransportError",
    "build_public_tool_name",
    "load_mcp_server_configs",
    "normalize_call_tool_result",
]
