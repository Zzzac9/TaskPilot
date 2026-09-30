"""把动态发现的 MCP Tool 适配为 TaskPilot async Tool。"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.types import CallToolResult
from pydantic import BaseModel

from taskpilot.config import redact_sensitive_values


_TOOL_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_FUNCTION_NAME_LENGTH = 64


class MCPToolExecutionError(RuntimeError):
    """MCP Server 以 is_error=true 返回工具业务失败。"""

    def __init__(self, tool_name: str, result: dict[str, Any]) -> None:
        self.tool_name = tool_name
        self.result = result
        detail = json.dumps(result, ensure_ascii=False, default=str)
        super().__init__(
            f"MCP 工具 {tool_name} 返回 is_error=true: {detail}"
        )


class MCPTransportError(RuntimeError):
    """MCP transport 或 session 调用本身失败。"""


def _sanitize_name_part(value: str) -> str:
    """把任意 MCP 标识稳定转换为 Function Calling 安全名称片段。"""

    normalized = _TOOL_NAME_PATTERN.sub("_", value).strip("_")
    if normalized:
        return normalized
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"unnamed_{digest}"


def build_public_tool_name(server_id: str, remote_tool_name: str) -> str:
    """生成 deterministic、最长 64 字符的 namespaced public name。"""

    raw_name = (
        f"{_sanitize_name_part(server_id)}__"
        f"{_sanitize_name_part(remote_tool_name)}"
    )
    if len(raw_name) <= _MAX_FUNCTION_NAME_LENGTH:
        return raw_name
    digest = hashlib.sha256(raw_name.encode("utf-8")).hexdigest()[:12]
    prefix_length = _MAX_FUNCTION_NAME_LENGTH - len(digest) - 1
    return f"{raw_name[:prefix_length]}_{digest}"


def _json_serializable(value: Any) -> Any:
    """把 MCP/Pydantic 值转换为稳定的 JSON-compatible representation。"""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=False)
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def normalize_call_tool_result(result: CallToolResult) -> dict[str, Any]:
    """保留 structured content 与所有 content block 类型。"""

    return {
        "structured_content": _json_serializable(result.structured_content),
        "content": [_json_serializable(block) for block in result.content],
        "is_error": result.is_error,
    }


@dataclass
class MCPToolAdapter:
    """持有长生命周期 ClientSession 的 TaskPilot Tool 实现。"""

    name: str
    remote_tool_name: str
    server_id: str
    description: str
    input_schema: dict[str, Any]
    session: ClientSession = field(repr=False)
    metadata: dict[str, Any] = field(default_factory=dict)

    async def invoke(self, arguments: dict[str, Any]) -> Any:
        """用远端原始名称调用 MCP Tool，并规范化 Observation。"""

        try:
            result = await self.session.call_tool(
                self.remote_tool_name,
                arguments=arguments,
            )
        except Exception as exc:
            detail = redact_sensitive_values(str(exc))
            raise MCPTransportError(
                f"MCP 工具 {self.name} transport/session 调用失败: "
                f"{type(exc).__name__}: {detail}"
            ) from exc
        if not isinstance(result, CallToolResult):
            raise MCPTransportError(
                f"MCP 工具 {self.name} 返回不支持的结果类型: "
                f"{type(result).__name__}"
            )

        normalized = normalize_call_tool_result(result)
        if result.is_error:
            raise MCPToolExecutionError(self.name, normalized)
        return normalized
