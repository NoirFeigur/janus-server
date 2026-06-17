"""Coverage for the MCP server factory (src/mcp/server.py).

``create_mcp_server`` lazily imports ``FastMCP`` and constructs the janus MCP
server. The import is deferred (inside the function) so importing the module is
cheap; the test calls the factory to cover the body and asserts the server is
named as expected.
"""

from __future__ import annotations

from src.mcp.server import create_mcp_server


def test_create_mcp_server_builds_named_server() -> None:
    server = create_mcp_server()
    assert server is not None
    # FastMCP stores the configured name; assert it round-trips.
    assert getattr(server, "name", None) == "janus"
