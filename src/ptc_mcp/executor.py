"""Execution engine: runs Python code with injected tool namespace."""

from __future__ import annotations

import asyncio
import contextlib
import io
import textwrap
import traceback
from typing import Any, Callable

from .config import ExecutionConfig


class ExecutionEngine:
    """Executes Python code strings with MCP tools injected as async functions."""

    def __init__(self, config: ExecutionConfig) -> None:
        self._config = config

    async def run(self, code: str, tool_namespace: dict[str, Callable[..., Any]]) -> str:
        """Execute code with the given tool namespace and return formatted output."""
        namespace: dict[str, Any] = dict(tool_namespace)

        # Wrap code in an async function to enable top-level await
        wrapped = "async def __main__():\n" + textwrap.indent(code, "    ")

        # Compile first to catch syntax errors early
        try:
            compiled = compile(wrapped, "<program>", "exec")
        except SyntaxError:
            return f"[Script execution failed]\n{traceback.format_exc()}"

        # Define __main__ in the namespace
        exec(compiled, namespace)

        # Run with stdout capture and timeout
        stdout_buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout_buffer):
                await asyncio.wait_for(
                    namespace["__main__"](),
                    timeout=self._config.timeout_seconds,
                )
        except asyncio.TimeoutError:
            return (
                f"[Script execution failed]\n"
                f"TimeoutError: Execution exceeded {self._config.timeout_seconds}s limit"
            )
        except Exception:
            return f"[Script execution failed]\n{traceback.format_exc()}"

        # Format output
        stdout = stdout_buffer.getvalue()
        if len(stdout.encode("utf-8")) > self._config.max_output_bytes:
            stdout = stdout[: self._config.max_output_bytes] + "\n... (truncated)"

        if stdout.strip():
            return f"[Script executed successfully]\n{stdout}"
        else:
            return "[Script executed successfully]\n(no output)"
