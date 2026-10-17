"""CLI entry point for the bundled Filesystem MCP STDIO server."""

import argparse
from pathlib import Path
from typing import Sequence

from taskpilot.filesystem_mcp.server import build_server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the TaskPilot Filesystem MCP")
    parser.add_argument(
        "--root",
        required=True,
        type=Path,
        help="Only this directory and its descendants may be accessed",
    )
    args = parser.parse_args(argv)
    build_server(args.root).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
