"""Official MCP SDK server exposing TaskPilot's sandboxed filesystem."""

from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from taskpilot.filesystem_mcp.storage import (
    FilesystemSandboxError,
    SandboxedFilesystem,
)


def build_server(root: Path) -> MCPServer:
    """Build a server bound to one immutable filesystem root."""

    filesystem = SandboxedFilesystem(root)
    server = MCPServer("taskpilot-filesystem")

    @server.tool(
        description=(
            "Read one UTF-8 text file beneath the configured filesystem root. "
            "The path must be relative. Do not use this for .docx files; use "
            "read_docx instead."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    def read_file(path: str) -> dict[str, str | int]:
        try:
            return filesystem.read_file(path)
        except (FilesystemSandboxError, OSError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        description=(
            "Create a new UTF-8 text file beneath the configured filesystem "
            "root. Existing files are never overwritten."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    def write_file(path: str, content: str) -> dict[str, str | int]:
        try:
            return filesystem.write_file(path, content)
        except (FilesystemSandboxError, OSError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        description=(
            "Read and verify the visible paragraphs of an existing Word .docx "
            "beneath the configured filesystem root. Use this after "
            "generate_docx instead of treating the Word file as UTF-8 text."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    def read_docx(path: str) -> dict[str, str | int | list[str]]:
        try:
            return filesystem.read_docx(path)
        except (FilesystemSandboxError, OSError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        description=(
            "Create a new Word .docx document beneath the configured filesystem "
            "root from a title, paragraphs, and optional source citations. "
            "Existing files are never overwritten."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    def generate_docx(
        path: str,
        title: str,
        paragraphs: list[str],
        sources: list[str] | None = None,
    ) -> dict[str, str | int]:
        try:
            return filesystem.generate_docx(path, title, paragraphs, sources)
        except (FilesystemSandboxError, OSError) as exc:
            raise ToolError(str(exc)) from exc

    return server
