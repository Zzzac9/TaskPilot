"""Host-side configuration for the bundled Filesystem MCP subprocess."""

import sys
from pathlib import Path

from taskpilot.mcp import MCPServerConfig, MCPTransport


def create_filesystem_mcp_config(
    root: Path,
    *,
    server_id: str = "filesystem",
) -> MCPServerConfig:
    """Create a trusted STDIO config for TaskPilot's first-party server."""

    sandbox_root = root.expanduser().resolve()
    return MCPServerConfig(
        server_id=server_id,
        transport=MCPTransport.STDIO,
        command=sys.executable,
        args=[
            "-u",
            "-m",
            "taskpilot.filesystem_mcp",
            "--root",
            str(sandbox_root),
        ],
        trust_tool_annotations=True,
    )
