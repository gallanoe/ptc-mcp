"""MCP server exposing the execute_program tool."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from .config import load_config
from .executor import ExecutionEngine
from .registry import ToolRegistry

logger = logging.getLogger(__name__)

CONFIG_PATH_ENV = "PTC_MCP_CONFIG"
DEFAULT_CONFIG_PATH = "config.yaml"


@asynccontextmanager
async def server_lifespan(_server: Server) -> AsyncIterator[dict[str, Any]]:
    """Initialize registry and executor, yield them, then clean up."""
    config_path = os.environ.get(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH)

    if os.path.exists(config_path):
        config = load_config(config_path)
        logger.info("Loaded config from %s", config_path)
    else:
        from .config import Config

        config = Config()
        logger.info("No config file found at %s, using defaults", config_path)

    registry = ToolRegistry(config)
    executor = ExecutionEngine(config.execution)

    await registry.initialize()
    logger.info("Registry initialized with %d tools", len(registry.get_namespace()))

    try:
        yield {"registry": registry, "executor": executor}
    finally:
        await registry.shutdown()
        logger.info("Registry shut down")


def create_server() -> Server:
    """Create and configure the MCP server."""
    server = Server("ptc-mcp", lifespan=server_lifespan)

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="execute_program",
                description=(
                    "Execute a Python program with access to MCP tools as async functions. "
                    "Tool calls within the script are dispatched to their respective MCP servers. "
                    "Only stdout (from print statements) is returned — intermediate tool results "
                    "do not enter the conversation context. Use this when a task involves 3+ tool "
                    "calls, loops, filtering, aggregation, or conditional logic based on intermediate "
                    "results. For single tool calls, call the tool directly. All tool functions "
                    "require `await`."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": (
                                "Python code to execute. MCP tools are available as async "
                                "functions using their namespaced names (e.g., "
                                "mcp__financial_data__query_financials). Use `await` for all "
                                "tool calls. Use `print()` to produce output."
                            ),
                        }
                    },
                    "required": ["code"],
                },
            ),
            types.Tool(
                name="inspect_tool",
                description=(
                    "Returns the schema and description of a tool available in "
                    "execute_program. Includes outputSchema if the upstream MCP server "
                    "defines one. Call this before writing a script if you need to "
                    "understand a tool's return format."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "tool_name": {
                            "type": "string",
                            "description": (
                                "Namespaced tool name "
                                "(e.g., mcp__financial_data__query_financials)"
                            ),
                        }
                    },
                    "required": ["tool_name"],
                },
            ),
            types.Tool(
                name="list_callable_tools",
                description=(
                    "Returns a JSON list of all tool names available for use inside "
                    "execute_program scripts. Use this to discover which tools are "
                    "callable before writing a program."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
        ]

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[types.TextContent]:
        ctx = server.request_context
        registry: ToolRegistry = ctx.lifespan_context["registry"]

        if name == "execute_program":
            executor: ExecutionEngine = ctx.lifespan_context["executor"]
            code = arguments.get("code", "")
            result = await executor.run(code, registry.get_namespace())
        elif name == "inspect_tool":
            tool_name = arguments.get("tool_name", "")
            result = registry.inspect_tool(tool_name)
        elif name == "list_callable_tools":
            result = registry.list_tool_names()
        else:
            raise ValueError(f"Unknown tool: {name}")

        return [types.TextContent(type="text", text=result)]

    return server


async def run_server() -> None:
    """Run the MCP server over stdio."""
    server = create_server()

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="ptc-mcp",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )
