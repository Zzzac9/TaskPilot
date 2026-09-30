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
