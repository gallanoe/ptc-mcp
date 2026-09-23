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
import time
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Callable

from . import sandbox
from .config import ExecutionConfig
from .registry import INTROSPECTION_NAMES

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
    # {"ok", "output", "result", "tool_calls": [...]} — the MCP structuredContent
    structured: dict[str, Any] = field(default_factory=dict)


@dataclass
class _CallRecord:
    tool: str
    args: str
    ok: bool
    ms: int
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"tool": self.tool, "args": self.args, "ok": self.ok, "ms": self.ms}
        if self.error is not None:
            d["error"] = self.error
        return d


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
        records: list[_CallRecord] = []
        started = time.monotonic()
        scratch = tempfile.mkdtemp(prefix="ptc-run-")
        try:
            try:
                argv = sandbox.build_command(self._config.sandbox, _RUNNER_SOURCE, scratch)
            except (RuntimeError, ValueError) as e:
                return self._compose(False, f"SandboxError: {e}", "", None, records, started)

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
                    self._session(proc, code, tool_namespace, records), timeout=timeout
                )
            except asyncio.TimeoutError:
                return self._compose(
                    False, f"TimeoutError: Execution exceeded {timeout}s limit", "", None,
                    records, started,
                )
            except _ProtocolError as e:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=2)
                stderr = await _drain(stderr_task)
                return self._compose(
                    False, _describe_crash(proc, str(e), stderr), "", None, records, started
                )
            finally:
                await _kill(proc)
                stderr_task.cancel()

            result = done.get("result") if done.get("has_result") else None
            has_result = bool(done.get("has_result"))
            return self._compose(
                bool(done.get("ok")), done.get("error") or "unknown error",
                done.get("output") or "", (result,) if has_result else None, records, started,
            )
        finally:
            sandbox.remove_scratch(scratch)

    def _compose(
        self,
        ok: bool,
        error: str,
        output: str,
        result: tuple[Any] | None,
        records: list[_CallRecord],
        started: float,
    ) -> ExecutionOutcome:
        """Build the text (for the model) and structured (for machines) outcome."""
        if ok:
            text = f"{SUCCESS_HEADER}\n{output if output.strip() else '(no output)'}"
            if result is not None:
                text = f"{text.rstrip()}\n--- result ---\n{json.dumps(result[0])}"
        else:
            text = f"{FAILURE_HEADER}\n{error}"
            if output.strip():
                text = f"{text.rstrip()}\n\n--- output before the error ---\n{output}"
        if self._config.trace == "summary" and records:
            text = f"{text.rstrip()}\n{_trace_summary(records, time.monotonic() - started)}"
        logger.info(
            "execute_program: ok=%s tool_calls=%d failed=%d",
            ok, len(records), sum(1 for r in records if not r.ok),
        )
        structured = {
            "ok": ok,
            "output": output,
            "result": result[0] if result is not None else None,
            "error": None if ok else error,
            "tool_calls": [r.as_dict() for r in records],
        }
        return ExecutionOutcome(ok, text, structured)

    async def _session(
        self,
        proc: asyncio.subprocess.Process,
        code: str,
        tool_namespace: dict[str, Callable[..., Any]],
        records: list[_CallRecord],
    ) -> dict[str, Any]:
        """Drive one child run until it reports ``done``."""
        cfg = self._config
        slots = asyncio.Semaphore(cfg.max_concurrent_calls)
        budget = {"used": 0}
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
            args = msg.get("args") if isinstance(msg.get("args"), dict) else {}
            handler = tool_namespace.get(name) if isinstance(name, str) else None
            counted = handler is not None and name not in INTROSPECTION_NAMES
            t0 = time.monotonic()
            reply: dict[str, Any]
            if handler is None:
                reply = {"type": "result", "id": call_id, "ok": False,
                         "error": f"unknown tool {name!r}"}
            elif counted and budget["used"] >= cfg.max_tool_calls:
                reply = {"type": "result", "id": call_id, "ok": False,
                         "error": f"tool-call budget exhausted (max_tool_calls={cfg.max_tool_calls})"}
            else:
                if counted:
                    budget["used"] += 1
                try:
                    async with slots:
                        value = await asyncio.wait_for(
                            handler(**args), timeout=cfg.tool_call_timeout_seconds
                        )
                    reply = {"type": "result", "id": call_id, "ok": True,
                             "value": _jsonable(value)}
                    size = len(json.dumps(reply["value"]).encode("utf-8"))
                    if size > cfg.max_tool_result_bytes:
                        reply = {"type": "result", "id": call_id, "ok": False,
                                 "error": f"'{name}' result is {size} bytes, over the "
                                          f"{cfg.max_tool_result_bytes}-byte limit; request less "
                                          "data (narrower dates, a limit, or a filter)"}
                except asyncio.TimeoutError:
                    reply = {"type": "result", "id": call_id, "ok": False,
                             "error": f"'{name}' timed out after {cfg.tool_call_timeout_seconds}s"}
                except Exception as e:  # noqa: BLE001 - surface every failure to the script
                    reply = {"type": "result", "id": call_id, "ok": False,
                             "error": str(e) or type(e).__name__}
            if counted or handler is None:
                records.append(_CallRecord(
                    tool=str(name), args=_short(args), ok=bool(reply["ok"]),
                    ms=round((time.monotonic() - t0) * 1000), error=reply.get("error"),
                ))
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


def _short(args: dict[str, Any], limit: int = 200) -> str:
    try:
        s = json.dumps(args, sort_keys=True)
    except (TypeError, ValueError):
        s = repr(args)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _trace_summary(records: list[_CallRecord], elapsed: float, max_failures: int = 5) -> str:
    failed = [r for r in records if not r.ok]
    line = f"[tool calls: {len(records)}"
    if failed:
        line += f", {len(failed)} failed"
    line += f"; {elapsed:.1f}s]"
    for r in failed[:max_failures]:
        line += f"\n  failed: {r.tool}: {r.error}"
    if len(failed) > max_failures:
        line += f"\n  ... {len(failed) - max_failures} more failures"
    return line


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
