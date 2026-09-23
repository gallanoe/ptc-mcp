"""MCP server exposing the execute_program tool."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.stdio import stdio_server

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


SERVER_VERSION = "0.2.0"


def _tool_definitions() -> list[types.Tool]:
    return [
        types.Tool(
            name="execute_program",
            description=(
                "Execute a Python program with access to MCP tools as async functions. "
                "The program runs in a sandboxed, standard-library-only interpreter with "
                "no network, no subprocesses, and no filesystem access except a temporary "
                "scratch directory (the working directory); tools are the only way to "
                "reach data. "
                "Tool calls within the script are dispatched to their respective MCP servers. "
                "Only stdout (from print statements) is returned — intermediate tool results "
                "do not enter the conversation context. Use this when a task involves 3+ tool "
                "calls, loops, filtering, aggregation, or conditional logic based on intermediate "
                "results. For single tool calls, call the tool directly. All tool functions "
                "require `await` and take keyword arguments. A failed tool call raises "
                "`ToolError` (catch it to continue). Results are parsed JSON; server notes "
                "(e.g. empty-result warnings) appear under a `_notes` key. Call "
                "`emit(value)` to return a structured JSON result. Helpers: "
                "`list_callable_tools()`, `inspect_tool(tool_name=...)`, `server_status()`. "
                "Each program has a tool-call budget and a per-call timeout."
            ),
            input_schema={
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
            input_schema={
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
            input_schema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


def create_server() -> Server:
    """Create and configure the MCP server."""

    async def handle_list_tools(
        ctx: ServerRequestContext[dict[str, Any]], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=_tool_definitions())

    async def handle_call_tool(
        ctx: ServerRequestContext[dict[str, Any]], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        registry: ToolRegistry = ctx.lifespan_context["registry"]
        name = params.name
        arguments = params.arguments or {}

        if name == "execute_program":
            executor: ExecutionEngine = ctx.lifespan_context["executor"]
            code = arguments.get("code", "")
            outcome = await executor.execute(code, registry.get_namespace())
            # Failed runs (script error, timeout, sandbox failure) are MCP errors
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=outcome.text)],
                structured_content=outcome.structured,
                is_error=not outcome.ok,
            )
        if name == "inspect_tool":
            text = registry.inspect_tool(arguments.get("tool_name", ""))
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=text)],
                is_error=text.startswith("[Tool not found]"),
            )
        if name == "list_callable_tools":
            content = [types.TextContent(type="text", text=registry.list_tool_names())]
            down = registry.unavailable_servers()
            if down:
                detail = "; ".join(f"{n}: {e}" for n, e in sorted(down.items()))
                content.append(types.TextContent(
                    type="text",
                    text=f"UNAVAILABLE SERVERS (their tools are not listed; reconnecting): {detail}",
                ))
            return types.CallToolResult(content=content)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Unknown tool: {name}")],
            is_error=True,
        )

    return Server(
        "ptc-mcp",
        version=SERVER_VERSION,
        lifespan=server_lifespan,
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )


async def run_server() -> None:
    """Run the MCP server over stdio."""
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
