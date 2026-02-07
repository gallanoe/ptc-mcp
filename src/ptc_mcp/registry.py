"""Tool registry: connects to downstream MCP servers and creates bridge handlers."""

from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Callable

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent

from .config import Config, ServerConfig
from .errors import ToolError

logger = logging.getLogger(__name__)


@dataclass
class RegisteredTool:
    name: str
    description: str
    parameters: dict  # inputSchema from upstream
    output_schema: dict | None  # outputSchema from upstream (non-standard, may be None)
    handler: Callable[..., Any]


class ToolRegistry:
    """Manages connections to downstream MCP servers and exposes bridged tool handlers."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._exit_stack = AsyncExitStack()
        self._tools: dict[str, RegisteredTool] = {}

    async def initialize(self) -> None:
        """Connect to all configured MCP servers and register bridge handlers."""
        for server_config in self._config.servers:
            try:
                session = await self._connect(server_config)
                tools_result = await session.list_tools()
                for tool in tools_result.tools:
                    namespaced = self._make_namespaced_name(
                        server_config.name, tool.name
                    )
                    if not self._is_allowed(namespaced):
                        logger.debug("Skipping blocked tool: %s", namespaced)
                        continue
                    handler = self._make_bridge_handler(session, tool.name, namespaced)
                    self._tools[namespaced] = RegisteredTool(
                        name=namespaced,
                        description=tool.description or "",
                        parameters=tool.inputSchema if tool.inputSchema else {},
                        output_schema=getattr(tool, "outputSchema", None),
                        handler=handler,
                    )
                logger.info(
                    "Connected to '%s': %d tools registered",
                    server_config.name,
                    sum(
                        1
                        for t in tools_result.tools
                        if self._is_allowed(
                            self._make_namespaced_name(server_config.name, t.name)
                        )
                    ),
                )
            except Exception:
                logger.warning(
                    "Failed to connect to '%s', skipping",
                    server_config.name,
                    exc_info=True,
                )

    async def _connect(self, server_config: ServerConfig) -> ClientSession:
        """Establish a client connection to a downstream MCP server."""
        if server_config.transport == "stdio":
            params = StdioServerParameters(
                command=server_config.command,
                args=server_config.args,
                env=server_config.env if server_config.env else None,
            )
            read_stream, write_stream = await self._exit_stack.enter_async_context(
                stdio_client(params)
            )
        elif server_config.transport == "sse":
            from mcp.client.sse import sse_client

            read_stream, write_stream = await self._exit_stack.enter_async_context(
                sse_client(server_config.url)
            )
        else:
            raise ValueError(f"Unknown transport: {server_config.transport}")

        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        return session

    @staticmethod
    def _make_namespaced_name(server_name: str, tool_name: str) -> str:
        """Create a namespaced tool name following Claude Code's convention."""
        safe_server = server_name.replace("-", "_")
        safe_tool = tool_name.replace("-", "_")
        return f"mcp__{safe_server}__{safe_tool}"

    def _is_allowed(self, namespaced: str) -> bool:
        """Check if a namespaced tool name passes allow/block filters."""
        tools_config = self._config.tools
        if tools_config.allow:
            return namespaced in tools_config.allow
        if tools_config.block:
            return namespaced not in tools_config.block
        return True

    def _make_bridge_handler(
        self, session: ClientSession, tool_name: str, namespaced: str
    ) -> Callable[..., Any]:
        """Create an async closure that bridges calls to a downstream MCP tool."""

        async def handler(**kwargs: Any) -> Any:
            try:
                result = await session.call_tool(tool_name, kwargs)
                return self._parse_mcp_result(result)
            except ToolError:
                raise
            except Exception as e:
                raise ToolError(f"'{namespaced}' failed: {e}") from e

        handler.__name__ = namespaced
        handler.__qualname__ = namespaced
        return handler

    @staticmethod
    def _parse_mcp_result(result: Any) -> Any:
        """Extract usable Python data from an MCP tool result."""
        texts = []
        for content in result.content:
            if isinstance(content, TextContent):
                texts.append(content.text)
            elif hasattr(content, "text"):
                texts.append(content.text)

        if not texts:
            return None

        combined = "\n".join(texts) if len(texts) > 1 else texts[0]

        try:
            return json.loads(combined)
        except (json.JSONDecodeError, TypeError):
            return combined

    def get_namespace(self) -> dict[str, Callable[..., Any]]:
        """Return tool namespace dict for injection into exec."""
        return {name: rt.handler for name, rt in self._tools.items()}

    def list_tool_names(self) -> str:
        """Return a JSON array of all registered tool names."""
        return json.dumps(sorted(self._tools.keys()))

    def inspect_tool(self, tool_name: str) -> str:
        """Return JSON schema and description for a registered tool."""
        tool = self._tools.get(tool_name)
        if not tool:
            return f"[Tool not found] '{tool_name}' is not available in execute_program"

        result: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.parameters,
        }

        if tool.output_schema:
            result["outputSchema"] = tool.output_schema
        else:
            result["outputSchema"] = None
            result["note"] = (
                "No output schema defined by the upstream server. "
                "Inspect the return value in your script "
                "(e.g., print(type(result), result[:1])) to determine the structure."
            )

        return json.dumps(result, indent=2)

    async def shutdown(self) -> None:
        """Close all downstream connections."""
        await self._exit_stack.aclose()
