"""A mock MCP server for integration testing using MCPServer."""

import json

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("mock-test-server")


@mcp.tool()
def add(a: int, b: int) -> str:
    """Add two numbers."""
    return json.dumps({"result": a + b})


@mcp.tool()
def greet(name: str) -> str:
    """Greet someone by name."""
    return f"Hello, {name}!"


@mcp.tool()
def get_data(key: str) -> str:
    """Get data for a key."""
    data = {
        "users": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}],
        "config": {"debug": True, "version": "1.0"},
    }
    result = data.get(key, f"No data for key: {key}")
    return json.dumps(result)


@mcp.tool()
def crash() -> str:
    """Kill this server process (for reconnect tests)."""
    import os

    os._exit(1)


@mcp.tool()
def fail() -> str:
    """Fail deliberately: returned to the client as an is_error result."""
    from mcp.server.mcpserver.exceptions import ToolError

    raise ToolError("mock failure")


@mcp.tool()
def boom() -> str:
    """Raise an unexpected exception: a JSON-RPC error with a generic message."""
    raise ValueError("internal detail")


if __name__ == "__main__":
    mcp.run()
