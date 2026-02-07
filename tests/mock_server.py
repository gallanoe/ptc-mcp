"""A mock MCP server for integration testing using FastMCP."""

import json

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mock-test-server")


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


if __name__ == "__main__":
    mcp.run()
