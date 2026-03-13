"""Allow running as ``python -m obscura.mcp_server``."""

from __future__ import annotations

import argparse


def main() -> None:
    """Entry point for the Obscura MCP server."""
    parser = argparse.ArgumentParser(
        description="Obscura MCP Server — proxy Obscura API as MCP tools",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8888,
        help="Port for HTTP/SSE transport (default: 8888)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host for HTTP/SSE transport (default: 0.0.0.0)",
    )
    args = parser.parse_args()

    from obscura.mcp_server.server import mcp

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
