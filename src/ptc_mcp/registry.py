"""Tool registry: connects to downstream MCP servers and creates bridge handlers.

Each downstream server is owned by a supervisor task that opens the connection,
registers the server's tools, and reconnects (with backoff) when the connection
fails or a call reports it lost. The supervisor enters and exits the transport
contexts in its own task, which anyio requires.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging

import anyio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Callable

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, TextContent

from .config import Config, ServerConfig
from .errors import ToolError

logger = logging.getLogger(__name__)

# Helpers injected into every script namespace alongside the bridged tools.
INTROSPECTION_NAMES = ("list_callable_tools", "inspect_tool", "server_status")

_CONNECT_TIMEOUT_SECONDS = 30
_MAX_BACKOFF_SECONDS = 60


@dataclass
class RegisteredTool:
    name: str
    description: str
    parameters: dict  # inputSchema from upstream
    output_schema: dict | None  # outputSchema from upstream (may be None)
    handler: Callable[..., Any]
    server: str = ""


class _ServerConnection:
    """One downstream server: its live session (if any) and a supervisor task."""

    def __init__(self, config: ServerConfig, on_connected: Callable[[_ServerConnection, list], None]):
        self.config = config
        self.session: Client | None = None
        self.error: str | None = None
        self.tool_count = 0
        self._on_connected = on_connected
        self._first_attempt = asyncio.Event()
        self._reconnect = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return self.config.name

    def start(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name=f"ptc-server-{self.name}")

    async def wait_first_attempt(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._first_attempt.wait(), timeout)
        except asyncio.TimeoutError:
            if self.session is None and self.error is None:
                self.error = f"no connection after {timeout:.0f}s"

    def request_reconnect(self, reason: str) -> None:
        if not self._reconnect.is_set():
            logger.warning("Server '%s' connection lost (%s); reconnecting", self.name, reason)
            self.session = None
            self.error = f"reconnecting after: {reason}"
            self._reconnect.set()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                logger.debug("Server '%s' supervisor ended with an error", self.name, exc_info=True)

    async def _supervise(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with AsyncExitStack() as stack:
                    session = await asyncio.wait_for(
                        stack.enter_async_context(_make_client(self.config)),
                        _CONNECT_TIMEOUT_SECONDS,
                    )
                    tools = await _list_all_tools(session)
                    self._reconnect.clear()
                    self.session, self.error = session, None
                    self._on_connected(self, tools)
                    self._first_attempt.set()
                    backoff = 1.0
                    await _wait_any(self._reconnect, self._stop)
            except Exception as e:  # noqa: BLE001 - any failure means "not connected"
                self.error = f"{type(e).__name__}: {e}"
                logger.warning("Server '%s' unavailable: %s", self.name, self.error)
            self.session = None
            self._first_attempt.set()
            if self._stop.is_set():
                break
            # Back off before reconnecting; wake early on stop.
            try:
                await asyncio.wait_for(self._stop.wait(), backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)


async def _wait_any(*events: asyncio.Event) -> None:
    waiters = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()


def _make_client(server_config: ServerConfig) -> Client:
    """A v2 ``Client`` for a downstream server (protocol negotiated automatically:
    2026-era discovery, falling back to the legacy initialize handshake)."""
    transport = server_config.transport
    if transport == "stdio":
        return Client(StdioServerParameters(
            command=server_config.command,
            args=server_config.args,
            env=server_config.env if server_config.env else None,
        ))
    if transport == "http":
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        http = create_mcp_http_client(headers=server_config.headers or None)
        return Client(streamable_http_client(server_config.url, http_client=http))
    if transport == "sse":
        from mcp.client.sse import sse_client

        return Client(sse_client(server_config.url, headers=server_config.headers or None))
    raise ValueError(f"Unknown transport: {transport}")


async def _list_all_tools(client: Client) -> list:
    """Every tool the server offers, following pagination cursors."""
    tools: list = []
    cursor: str | None = None
    while True:
        page = await client.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if not cursor:
            return tools


class ToolRegistry:
    """Manages connections to downstream MCP servers and exposes bridged tool handlers."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._tools: dict[str, RegisteredTool] = {}
        self._connections: dict[str, _ServerConnection] = {}

    async def initialize(self) -> None:
        """Start a supervisor per server and wait for each first connection attempt."""
        for server_config in self._config.servers:
            conn = _ServerConnection(server_config, self._register_server_tools)
            self._connections[server_config.name] = conn
            conn.start()
        await asyncio.gather(
            *(c.wait_first_attempt(_CONNECT_TIMEOUT_SECONDS + 5) for c in self._connections.values())
        )

    def _register_server_tools(self, conn: _ServerConnection, tools: list) -> None:
        """(Re)register one server's tools after it (re)connects."""
        for name in [n for n, t in self._tools.items() if t.server == conn.name]:
            del self._tools[name]
        count = 0
        for tool in tools:
            namespaced = self._make_namespaced_name(conn.name, tool.name)
            if not self._is_allowed(namespaced):
                logger.debug("Skipping filtered tool: %s", namespaced)
                continue
            self._tools[namespaced] = RegisteredTool(
                name=namespaced,
                description=tool.description or "",
                parameters=tool.input_schema if tool.input_schema else {},
                output_schema=tool.output_schema,
                handler=self._make_bridge_handler(conn, tool.name, namespaced),
                server=conn.name,
            )
            count += 1
        conn.tool_count = count
        logger.info("Connected to '%s': %d tools registered", conn.name, count)

    @staticmethod
    def _make_namespaced_name(server_name: str, tool_name: str) -> str:
        """Create a namespaced tool name following Claude Code's convention."""
        safe_server = server_name.replace("-", "_")
        safe_tool = tool_name.replace("-", "_")
        return f"mcp__{safe_server}__{safe_tool}"

    def _is_allowed(self, namespaced: str) -> bool:
        """Check a namespaced tool name against allow/block lists (glob patterns)."""
        tools_config = self._config.tools
        if tools_config.allow:
            return any(fnmatch.fnmatchcase(namespaced, p) for p in tools_config.allow)
        if tools_config.block:
            return not any(fnmatch.fnmatchcase(namespaced, p) for p in tools_config.block)
        return True

    def _make_bridge_handler(
        self, conn: _ServerConnection, tool_name: str, namespaced: str
    ) -> Callable[..., Any]:
        """Create an async closure that bridges calls to a downstream MCP tool."""

        async def handler(**kwargs: Any) -> Any:
            session = conn.session
            if session is None:
                raise ToolError(
                    f"'{namespaced}' unavailable: server '{conn.name}' is not connected"
                    f" ({conn.error or 'connecting'})"
                )
            try:
                result = await session.call_tool(tool_name, kwargs)
            except MCPError as e:
                if e.code != CONNECTION_CLOSED:
                    # Protocol-level error from a live server (bad arguments, unknown tool)
                    raise ToolError(f"'{namespaced}' failed: {e}") from e
                conn.request_reconnect(str(e))
                raise ToolError(
                    f"'{namespaced}' failed: connection to '{conn.name}' closed; "
                    "reconnecting — retry shortly"
                ) from e
            except Exception as e:  # noqa: BLE001 - classified below
                if _is_transport_failure(e):
                    conn.request_reconnect(f"{type(e).__name__}: {e}")
                    raise ToolError(
                        f"'{namespaced}' failed: connection to '{conn.name}' lost ({e}); "
                        "reconnecting — retry shortly"
                    ) from e
                if _is_schema_mismatch(e):
                    # The client validates results against the tool's declared outputSchema;
                    # a mismatch is bad data from a healthy server, not a lost connection.
                    raise ToolError(
                        f"'{namespaced}' returned data that does not match its declared "
                        f"output schema: {e}"
                    ) from e
                raise ToolError(f"'{namespaced}' failed: {type(e).__name__}: {e}") from e
            return self._parse_mcp_result(result, namespaced)

        handler.__name__ = namespaced
        handler.__qualname__ = namespaced
        return handler

    @staticmethod
    def _parse_mcp_result(result: Any, namespaced: str = "tool") -> Any:
        """Extract usable Python data from an MCP tool result.

        - ``is_error`` results raise ``ToolError`` (never returned as data).
        - ``structured_content`` (a JSON object) is preferred when present. Text
          items that are not the JSON rendering of it are notes from the server
          (e.g. "EMPTY RESULT ..."); they are kept under a ``_notes`` key.
        - Otherwise the text is JSON-decoded when possible, else returned as-is.
        """
        items = getattr(result, "content", None) or []
        texts = [c.text for c in items if isinstance(c, TextContent) or hasattr(c, "text")]
        # Non-text content (images, audio, resources) cannot cross into the script; say so
        # instead of dropping it silently.
        omitted = [_describe_non_text(c) for c in items
                   if not (isinstance(c, TextContent) or hasattr(c, "text"))]

        if getattr(result, "is_error", False) is True:
            detail = " ".join(t.strip() for t in texts if t.strip()) or "no details"
            raise ToolError(f"'{namespaced}' returned an error: {detail}")

        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            if any(_is_json_of(t, structured) for t in texts) or set(structured) != {"result"}:
                # A genuine object result (the text is its rendering, or prose).
                notes = [t for t in texts if not _is_json_of(t, structured)]
                return _attach_notes(dict(structured), notes + omitted)
            # FastMCP wraps non-object returns as {"result": value}; unwrap it.
            value = structured["result"]
            notes = [t for t in texts if t != value and not _is_json_of(t, value)]
            if isinstance(value, str):
                value = _json_or_text(value)
            return _attach_notes(value, notes + omitted)

        if not texts:
            return {"data": None, "_notes": omitted} if omitted else None
        if len(texts) == 1:
            return _attach_notes(_json_or_text(texts[0]), omitted)

        parsed, notes = [], []
        for t in texts:
            try:
                parsed.append(json.loads(t))
            except (json.JSONDecodeError, TypeError):
                notes.append(t)
        if len(parsed) == 1 and notes:
            return _attach_notes(parsed[0], notes + omitted)
        return _attach_notes(_json_or_text("\n".join(texts)), omitted)

    def get_namespace(self) -> dict[str, Callable[..., Any]]:
        """Return tool namespace dict for injection into a script."""
        ns: dict[str, Callable[..., Any]] = {
            name: rt.handler for name, rt in self._tools.items()
        }

        async def list_callable_tools() -> list[str]:
            """Return sorted list of all registered tool names."""
            return sorted(self._tools.keys())

        async def _inspect_tool_impl(*, tool_name: str) -> dict | str:
            """Return parsed schema dict for a tool, or error string if not found."""
            raw = self.inspect_tool(tool_name)
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return raw

        async def server_status() -> dict[str, dict[str, Any]]:
            """Connection state of every configured downstream server."""
            return self.server_status()

        ns["list_callable_tools"] = list_callable_tools
        ns["inspect_tool"] = _inspect_tool_impl
        ns["server_status"] = server_status
        return ns

    def server_status(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "connected": conn.session is not None,
                "tools": conn.tool_count if conn.session is not None else 0,
                "error": None if conn.session is not None else conn.error,
            }
            for name, conn in self._connections.items()
        }

    def unavailable_servers(self) -> dict[str, str]:
        return {
            name: (conn.error or "not connected")
            for name, conn in self._connections.items()
            if conn.session is None
        }

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
        """Stop every server supervisor (closing its connection)."""
        await asyncio.gather(*(c.stop() for c in self._connections.values()))


def _is_transport_failure(e: BaseException) -> bool:
    """True for errors that mean the connection itself is gone (so reconnecting helps)."""
    return isinstance(e, (
        anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream,
        ConnectionError, EOFError, OSError,
    )) or (isinstance(e, RuntimeError) and "async context manager" in str(e))


def _is_schema_mismatch(e: BaseException) -> bool:
    """The SDK client's outputSchema validation failures (raised as RuntimeError)."""
    msg = str(e)
    return isinstance(e, RuntimeError) and (
        "Invalid structured content returned by tool" in msg
        or "has an output schema but did not return structured content" in msg
        or "Invalid schema for tool" in msg
    )


def _describe_non_text(content: Any) -> str:
    kind = getattr(content, "type", type(content).__name__)
    mime = getattr(content, "mime_type", None) or getattr(
        getattr(content, "resource", None), "mime_type", None)
    return f"[non-text content omitted: {kind}{f' ({mime})' if mime else ''}]"


def _is_json_of(text: str, value: Any) -> bool:
    try:
        return json.loads(text) == value
    except (json.JSONDecodeError, TypeError):
        return False


def _attach_notes(value: Any, notes: list[str]) -> Any:
    """Keep server notes next to the data: a ``_notes`` key on objects, or a
    ``{"data": ..., "_notes": [...]}`` wrapper for anything else."""
    if not notes:
        return value
    if isinstance(value, dict):
        return {**value, "_notes": notes}
    return {"data": value, "_notes": notes}


def _json_or_text(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
