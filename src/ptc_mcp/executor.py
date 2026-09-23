"""Execution engine: runs each program in a sandboxed child interpreter.

The server process never executes script code. Each run gets a fresh child
(see ``_runner.py``) launched under the configured sandbox with an empty
environment and a private scratch directory. Tool calls from the script come
back over the child's stdout as JSON messages; the parent dispatches them to
the injected tool namespace (which alone holds the MCP sessions) and writes the
results to the child's stdin. The wall-clock timeout kills the child, so it
also stops CPU-bound code.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import tempfile
from dataclasses import dataclass
from importlib import resources
from typing import Any, Callable

from . import sandbox
from .config import ExecutionConfig

logger = logging.getLogger(__name__)

SUCCESS_HEADER = "[Script executed successfully]"
FAILURE_HEADER = "[Script execution failed]"

_RUNNER_SOURCE = resources.files(__package__).joinpath("_runner.py").read_text("utf-8")

# Largest single message accepted from the child (tool-call args or the final
# output). Output is capped in the child; this bounds a misbehaving script.
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
# Child stderr is only read for diagnostics (e.g. the interpreter failing to
# start under the sandbox).
_MAX_STDERR_BYTES = 16 * 1024


class _ProtocolError(Exception):
    pass


@dataclass
class ExecutionOutcome:
    ok: bool
    text: str


class ExecutionEngine:
    """Executes Python programs in a sandboxed child with MCP tools injected."""

    def __init__(self, config: ExecutionConfig) -> None:
        self._config = config

    async def run(self, code: str, tool_namespace: dict[str, Callable[..., Any]]) -> str:
        """Execute code and return the formatted output text."""
        return (await self.execute(code, tool_namespace)).text

    async def execute(
        self, code: str, tool_namespace: dict[str, Callable[..., Any]]
    ) -> ExecutionOutcome:
        """Execute code; ``ok`` is False for script errors, timeouts, and sandbox failures."""
        timeout = self._config.timeout_seconds
        scratch = tempfile.mkdtemp(prefix="ptc-run-")
        try:
            try:
                argv = sandbox.build_command(self._config.sandbox, _RUNNER_SOURCE, scratch)
            except (RuntimeError, ValueError) as e:
                return _failure(f"SandboxError: {e}")

            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=scratch,
                env=sandbox.child_env(scratch),
                start_new_session=True,
                limit=_MAX_MESSAGE_BYTES,
            )
            stderr_task = asyncio.create_task(_read_capped(proc.stderr, _MAX_STDERR_BYTES))
            try:
                done = await asyncio.wait_for(
                    self._session(proc, code, tool_namespace), timeout=timeout
                )
            except asyncio.TimeoutError:
                return _failure(f"TimeoutError: Execution exceeded {timeout}s limit")
            except _ProtocolError as e:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=2)
                stderr = await _drain(stderr_task)
                return _failure(_describe_crash(proc, str(e), stderr))
            finally:
                await _kill(proc)
                stderr_task.cancel()

            if done.get("ok"):
                output = done.get("output") or ""
                return ExecutionOutcome(
                    True, f"{SUCCESS_HEADER}\n{output if output.strip() else '(no output)'}"
                )
            text = done.get("error") or "unknown error"
            output = done.get("output") or ""
            if output.strip():
                text = f"{text.rstrip()}\n\n--- output before the error ---\n{output}"
            return _failure(text)
        finally:
            sandbox.remove_scratch(scratch)

    async def _session(
        self,
        proc: asyncio.subprocess.Process,
        code: str,
        tool_namespace: dict[str, Callable[..., Any]],
    ) -> dict[str, Any]:
        """Drive one child run until it reports ``done``."""
        assert proc.stdin is not None and proc.stdout is not None
        write_lock = asyncio.Lock()

        async def send(msg: dict[str, Any]) -> None:
            data = (json.dumps(msg) + "\n").encode("utf-8")
            async with write_lock:
                proc.stdin.write(data)
                await proc.stdin.drain()

        limits = {
            "max_output_bytes": self._config.max_output_bytes,
            # CPU backstop slightly above the wall clock; the parent kill is primary
            "cpu_seconds": int(self._config.timeout_seconds) + 2,
            "file_size_bytes": 64 * 1024 * 1024,
            "open_files": 256,
        }
        try:
            await send(
                {"type": "start", "code": code, "tools": sorted(tool_namespace), "limits": limits}
            )
        except (BrokenPipeError, ConnectionResetError) as e:
            raise _ProtocolError(f"child exited before start: {e}") from e

        calls: set[asyncio.Task[None]] = set()

        async def handle_call(msg: dict[str, Any]) -> None:
            call_id = msg.get("id")
            name = msg.get("tool")
            handler = tool_namespace.get(name) if isinstance(name, str) else None
            if handler is None:
                reply = {"type": "result", "id": call_id, "ok": False,
                         "error": f"unknown tool {name!r}"}
            else:
                args = msg.get("args") or {}
                try:
                    value = await handler(**args)
                    reply = {"type": "result", "id": call_id, "ok": True,
                             "value": _jsonable(value)}
                except Exception as e:  # noqa: BLE001 - surface every failure to the script
                    reply = {"type": "result", "id": call_id, "ok": False,
                             "error": str(e) or type(e).__name__}
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await send(reply)

        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError as e:  # message over the limit
                    raise _ProtocolError(f"child message too large: {e}") from e
                if not line:
                    raise _ProtocolError("child exited without reporting a result")
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as e:
                    raise _ProtocolError(f"malformed message from child: {e}") from e
                kind = msg.get("type") if isinstance(msg, dict) else None
                if kind == "call":
                    task = asyncio.create_task(handle_call(msg))
                    calls.add(task)
                    task.add_done_callback(calls.discard)
                elif kind == "done":
                    return msg
                else:
                    raise _ProtocolError(f"unexpected message from child: {kind!r}")
        finally:
            for task in calls:
                task.cancel()


def _failure(text: str) -> ExecutionOutcome:
    return ExecutionOutcome(False, f"{FAILURE_HEADER}\n{text}")


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


async def _read_capped(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    if stream is None:
        return b""
    buf = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(buf)
        if len(buf) < limit:
            buf.extend(chunk[: limit - len(buf)])


async def _drain(task: asyncio.Task[bytes]) -> bytes:
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=1)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return b""


def _describe_crash(proc: asyncio.subprocess.Process, reason: str, stderr: bytes) -> str:
    code = proc.returncode
    parts = [f"ExecutionError: {reason}"]
    if code is not None and code < 0:
        with contextlib.suppress(ValueError):
            parts.append(f"(child killed by {signal.Signals(-code).name})")
    elif code is not None:
        parts.append(f"(child exit status {code})")
    if stderr.strip():
        parts.append(stderr.decode("utf-8", "replace").strip())
    return "\n".join(parts)


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill the child's whole process group and reap it."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=5)
