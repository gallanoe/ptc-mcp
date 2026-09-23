"""Child-process runner for execute_program.

This file is NOT imported by the server. Its source is passed to a fresh
interpreter (``python -I -S -c <source>``) that runs inside the sandbox, so it
may only use the standard library.

Protocol (newline-delimited JSON):
  parent -> child  {"type": "start", "code": str, "tools": [str], "limits": {...}}
  child  -> parent {"type": "call", "id": int, "tool": str, "args": {...}}
  parent -> child  {"type": "result", "id": int, "ok": bool, "value": ..., "error": str}
  child  -> parent {"type": "done", "ok": bool, "output": str, "error": str | null,
                    "has_result": bool, "result": ...}

Scripts can call ``emit(value)`` to return a JSON-serializable structured
result alongside printed output (the last call wins).

The RPC pipes are moved off fds 0/1 before any user code runs, and fds 0/1 are
pointed at /dev/null, so nothing the script prints or writes can corrupt the
channel.
"""

import ast
import asyncio
import contextlib
import io
import json
import linecache
import os
import resource
import sys
import traceback

PROGRAM = "<program>"


class ToolError(Exception):
    """Raised inside the script when a bridged tool call fails."""


class _CappedWriter(io.TextIOBase):
    """Text sink that keeps at most ``limit`` UTF-8 bytes and notes truncation."""

    def __init__(self, limit):
        self._limit = limit
        self._size = 0
        self._parts = []
        self.truncated = False

    def writable(self):
        return True

    def write(self, s):
        if not isinstance(s, str):
            raise TypeError("write() argument must be str")
        if self.truncated:
            return len(s)
        data = s.encode("utf-8", "replace")
        room = self._limit - self._size
        if len(data) > room:
            self._parts.append(data[:room].decode("utf-8", "ignore"))
            self._size = self._limit
            self.truncated = True
        else:
            self._parts.append(s)
            self._size += len(data)
        return len(s)

    def getvalue(self):
        return "".join(self._parts)


def _set_limits(limits):
    """Lower (soft and hard) resource limits before user code runs."""
    wanted = {
        "RLIMIT_CPU": limits.get("cpu_seconds"),
        "RLIMIT_FSIZE": limits.get("file_size_bytes"),
        "RLIMIT_NOFILE": limits.get("open_files"),
        "RLIMIT_CORE": 0,
    }
    for name, value in wanted.items():
        if value is None or not hasattr(resource, name):
            continue
        res = getattr(resource, name)
        try:
            soft, hard = resource.getrlimit(res)
            new = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(res, (new, new))
        except (ValueError, OSError):
            pass  # not supported on this platform; the wall-clock kill still applies


def _format_error(exc):
    """Traceback limited to the user's program frames."""
    if isinstance(exc, SyntaxError):
        return "".join(traceback.format_exception_only(type(exc), exc))
    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename != PROGRAM:
        tb = tb.tb_next
    return "".join(traceback.format_exception(type(exc), exc, tb or exc.__traceback__))


class _Rpc:
    def __init__(self, reader, out_fd):
        self._reader = reader
        self._out_fd = out_fd
        self._next_id = 0
        self._pending = {}
        self._broken = None  # set when the channel from the parent fails

    def send(self, msg):
        data = (json.dumps(msg) + "\n").encode("utf-8")
        view = memoryview(data)
        while view:
            n = os.write(self._out_fd, view)
            view = view[n:]

    async def recv(self):
        line = await self._reader.readline()
        if not line:
            raise EOFError("parent closed the channel")
        return json.loads(line)

    def _fail_pending(self, reason):
        self._broken = reason
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ToolError(reason))
        self._pending.clear()

    async def dispatch_results(self):
        """Resolve pending call futures as results arrive."""
        while True:
            try:
                msg = await self.recv()
            except EOFError:
                self._fail_pending("tool channel closed")
                return
            except Exception as e:  # oversized or malformed message: the channel is unusable
                self._fail_pending(f"tool channel failed: {type(e).__name__}: {e}")
                return
            fut = self._pending.pop(msg.get("id"), None)
            if fut is None or fut.done():
                continue
            if msg.get("ok"):
                fut.set_result(msg.get("value"))
            else:
                fut.set_exception(ToolError(msg.get("error") or "tool call failed"))

    async def call(self, tool, args):
        if self._broken:
            raise ToolError(self._broken)
        self._next_id += 1
        call_id = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        try:
            payload = json.dumps(args)  # fail fast on non-JSON arguments
        except (TypeError, ValueError) as e:
            self._pending.pop(call_id, None)
            raise TypeError(f"arguments to {tool} must be JSON-serializable: {e}") from None
        self.send({"type": "call", "id": call_id, "tool": tool, "args": json.loads(payload)})
        return await fut


def _make_stub(rpc, name):
    async def stub(*args, **kwargs):
        if args:
            raise TypeError(f"{name}() takes keyword arguments only")
        return await rpc.call(name, kwargs)

    stub.__name__ = name
    stub.__qualname__ = name
    return stub


async def _main(in_fd, out_fd):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(in_fd, "rb", 0)
    )
    rpc = _Rpc(reader, out_fd)

    start = await rpc.recv()
    code = start["code"]
    limits = start.get("limits", {})
    _set_limits(limits)

    max_output = int(limits.get("max_output_bytes", 65536))
    emitted = {"set": False, "value": None}

    def emit(value):
        """Return ``value`` (JSON-serializable) as the program's structured result."""
        try:
            data = json.dumps(value)
        except (TypeError, ValueError) as e:
            raise TypeError(f"emit() value must be JSON-serializable: {e}") from None
        size = len(data.encode("utf-8"))
        if size > max_output:
            raise ValueError(f"emit() value is {size} bytes; the limit is {max_output}")
        emitted["set"], emitted["value"] = True, json.loads(data)

    namespace = {"__name__": "__main__", "ToolError": ToolError, "emit": emit}
    for name in start.get("tools", []):
        namespace[name] = _make_stub(rpc, name)

    results_task = asyncio.create_task(rpc.dispatch_results())
    out = _CappedWriter(max_output)
    ok, error = True, None
    try:
        linecache.cache[PROGRAM] = (len(code), None, code.splitlines(True), PROGRAM)
        compiled = compile(code, PROGRAM, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            result = eval(compiled, namespace)
            if asyncio.iscoroutine(result):
                await result
    except BaseException as exc:  # noqa: BLE001 - report everything, incl. SystemExit
        ok, error = False, _format_error(exc)
    finally:
        results_task.cancel()

    output = out.getvalue()
    if out.truncated:
        output += "\n... (truncated)"
    rpc.send({
        "type": "done", "ok": ok, "output": output, "error": error,
        "has_result": emitted["set"], "result": emitted["value"],
    })
    # Exit immediately: tasks the script left running must not delay or
    # outlive the run (asyncio.run would wait on their cancellation).
    os._exit(0)


def _bootstrap():
    # Move the RPC channel off stdin/stdout, then point 0/1 at /dev/null.
    in_fd, out_fd = os.dup(0), os.dup(1)
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.close(devnull)
    sys.stdin = open(os.devnull)
    asyncio.run(_main(in_fd, out_fd))
    os._exit(0)


_bootstrap()
