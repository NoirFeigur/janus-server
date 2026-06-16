from typing import Any


def create_mcp_server() -> Any:
    from mcp.server.fastmcp import FastMCP

    return FastMCP(name="janus", stateless_http=True)
