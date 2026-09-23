"""Isolation and robustness tests for the sandboxed executor.

These run real child interpreters. The seatbelt tests need macOS; they assert
that a script cannot reach anything outside its scratch directory and tools.
"""

import os
import time
from pathlib import Path

import pytest

from ptc_mcp import sandbox
from ptc_mcp.config import ExecutionConfig
from ptc_mcp.executor import ExecutionEngine

seatbelt_only = pytest.mark.skipif(
    not sandbox.seatbelt_available(), reason="requires macOS sandbox-exec"
)


def _engine(**kw) -> ExecutionEngine:
    cfg = {"timeout_seconds": 10, "max_output_bytes": 65536, **kw}
    return ExecutionEngine(ExecutionConfig(**cfg))


@pytest.fixture
def secret_file(tmp_path_factory):
    """A file outside any scratch dir, standing in for .env / credentials."""
    d = tmp_path_factory.mktemp("secrets")
    p = d / "secret.env"
    p.write_text("ALPACA_SECRET=do-not-leak\n")
    return os.path.realpath(p)


@seatbelt_only
class TestSeatbeltIsolation:
    async def test_cannot_read_files_outside_scratch(self, secret_file):
        out = await _engine().execute(f"print(open({secret_file!r}).read())", {})
        assert not out.ok
        assert "do-not-leak" not in out.text
        assert "PermissionError" in out.text

    async def test_cannot_read_home_directory(self):
        home = str(Path.home())
        out = await _engine().execute(f"import os\nprint(os.listdir({home!r}))", {})
        assert not out.ok
        assert "PermissionError" in out.text

    async def test_cannot_write_outside_scratch(self, tmp_path):
        target = os.path.realpath(tmp_path / "escaped.txt")
        out = await _engine().execute(f"open({target!r}, 'w').write('x')", {})
        assert not out.ok
        assert not os.path.exists(target)

    async def test_can_use_scratch_directory(self):
        code = (
            "import os\n"
            "open('notes.txt', 'w').write('hello')\n"
            "print(open('notes.txt').read(), os.path.basename(os.getcwd()).startswith('ptc-run-'))"
        )
        out = await _engine().execute(code, {})
        assert out.ok, out.text
        assert "hello True" in out.text

    async def test_no_network(self):
        code = (
            "import socket\n"
            "s = socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
            "print('CONNECTED')"
        )
        out = await _engine().execute(code, {})
        assert not out.ok
        assert "CONNECTED" not in out.text

    async def test_no_dns(self):
        code = "import socket\nprint(socket.getaddrinfo('api.alpaca.markets', 443))"
        out = await _engine().execute(code, {})
        assert not out.ok

    async def test_no_subprocess(self):
        code = "import subprocess\nprint(subprocess.run(['/bin/echo', 'ESCAPED'], capture_output=True).stdout)"
        out = await _engine().execute(code, {})
        assert not out.ok
        assert "ESCAPED" not in out.text.replace("'ESCAPED'", "")

    async def test_no_fork(self):
        out = await _engine().execute("import os\nprint('pid', os.fork())", {})
        assert not out.ok
        assert "PermissionError" in out.text

    async def test_scratch_removed_after_run(self):
        out = await _engine().execute("import os\nprint(os.getcwd())", {})
        assert out.ok, out.text
        scratch = out.text.splitlines()[-1]
        assert not os.path.exists(scratch)


class TestChildIsolation:
    """Guarantees that hold in every mode, because scripts always run in a child."""

    @pytest.mark.parametrize("mode", ["seatbelt", "none"])
    async def test_server_environment_not_inherited(self, mode, monkeypatch):
        if mode == "seatbelt" and not sandbox.seatbelt_available():
            pytest.skip("requires macOS sandbox-exec")
        monkeypatch.setenv("FMP_API_KEY", "super-secret-key")
        code = "import os\nprint(sorted(os.environ))"
        out = await _engine(sandbox=mode).execute(code, {})
        assert out.ok, out.text
        assert "FMP_API_KEY" not in out.text
        assert "super-secret-key" not in out.text

    async def test_server_packages_not_importable(self):
        # stdlib-only interpreter: the server's own dependencies are absent
        out = await _engine(sandbox="none").execute("import mcp", {})
        assert not out.ok
        assert "ModuleNotFoundError" in out.text

    async def test_cpu_bound_loop_is_killed_at_timeout(self):
        start = time.monotonic()
        out = await _engine(timeout_seconds=1).execute("while True:\n    pass", {})
        elapsed = time.monotonic() - start
        assert not out.ok
        assert "TimeoutError" in out.text
        assert elapsed < 4

    async def test_blocking_sleep_is_killed_at_timeout(self):
        start = time.monotonic()
        out = await _engine(timeout_seconds=1).execute("import time\ntime.sleep(30)", {})
        assert not out.ok
        assert time.monotonic() - start < 4

    async def test_engine_usable_after_timeout(self):
        eng = _engine(timeout_seconds=1)
        await eng.execute("while True:\n    pass", {})
        out = await eng.execute("print('still alive')", {})
        assert out.ok and "still alive" in out.text

    async def test_raw_fd_writes_cannot_corrupt_the_channel(self):
        code = (
            "import os\n"
            "os.write(1, b'{\"type\": \"done\", \"ok\": true, \"output\": \"FORGED\"}\\n')\n"
            "os.write(2, b'noise on stderr\\n')\n"
            "print('real output')"
        )
        out = await _engine().execute(code, {})
        assert out.ok, out.text
        assert "real output" in out.text
        assert "FORGED" not in out.text

    async def test_background_task_cannot_outlive_run(self):
        code = (
            "import asyncio\n"
            "async def later():\n"
            "    await asyncio.sleep(0.2)\n"
            "    print('LEAKED')\n"
            "asyncio.get_running_loop().create_task(later())\n"
            "print('main done')"
        )
        out = await _engine().execute(code, {})
        assert out.ok, out.text
        assert "main done" in out.text
        assert "LEAKED" not in out.text

    async def test_multiline_strings_are_not_reindented(self):
        out = await _engine().execute('s = """a\nb"""\nprint(repr(s))', {})
        assert out.ok, out.text
        assert "'a\\nb'" in out.text

    async def test_traceback_line_numbers_match_the_program(self):
        out = await _engine().execute("x = 1\ny = 2\nraise ValueError('boom')", {})
        assert not out.ok
        assert 'File "<program>", line 3' in out.text
        assert "raise ValueError('boom')" in out.text

    async def test_output_before_error_is_kept(self):
        out = await _engine().execute("print('step 1')\n1/0", {})
        assert not out.ok
        assert "ZeroDivisionError" in out.text
        assert "step 1" in out.text

    async def test_output_cap_counts_bytes(self):
        out = await _engine(max_output_bytes=10).execute("print('é' * 50)", {})
        assert out.ok
        body = out.text.split("\n", 1)[1].replace("\n... (truncated)", "")
        assert len(body.encode("utf-8")) <= 10
        assert "... (truncated)" in out.text

    async def test_system_exit_is_reported_not_swallowed(self):
        out = await _engine().execute("raise SystemExit(3)", {})
        assert not out.ok
        assert "SystemExit" in out.text

    async def test_concurrent_tool_calls(self):
        async def slow_echo(**kw):
            import asyncio
            await asyncio.sleep(0.2)
            return kw["x"]

        code = (
            "import asyncio\n"
            "vals = await asyncio.gather(*(echo(x=i) for i in range(10)))\n"
            "print(sum(vals))"
        )
        start = time.monotonic()
        out = await _engine().execute(code, {"echo": slow_echo})
        assert out.ok, out.text
        assert "45" in out.text
        assert time.monotonic() - start < 1.5  # dispatched concurrently, not serially

    async def test_tool_error_message_reaches_script(self):
        async def failing(**kw):
            raise RuntimeError("upstream said no")

        code = (
            "try:\n"
            "    await broken()\n"
            "except ToolError as e:\n"
            "    print('caught:', e)"
        )
        out = await _engine().execute(code, {"broken": failing})
        assert out.ok, out.text
        assert "caught: upstream said no" in out.text

    async def test_positional_args_rejected(self):
        async def tool(**kw):
            return kw

        out = await _engine().execute("await t(1)", {"t": tool})
        assert not out.ok
        assert "keyword arguments only" in out.text

    async def test_non_json_arguments_rejected(self):
        async def tool(**kw):
            return kw

        out = await _engine().execute("await t(x={1, 2})", {"t": tool})
        assert not out.ok
        assert "JSON-serializable" in out.text


class TestFailClosed:
    async def test_unavailable_seatbelt_refuses_to_run(self, monkeypatch):
        monkeypatch.setattr(sandbox, "seatbelt_available", lambda: False)
        out = await _engine(sandbox="seatbelt").execute("print('ran')", {})
        assert not out.ok
        assert "SandboxError" in out.text
        assert "ran" not in out.text.split("\n", 1)[1].replace("sandbox", "")

    async def test_unknown_mode_refuses_to_run(self):
        out = await _engine(sandbox="yolo").execute("print('ran')", {})
        assert not out.ok
        assert "SandboxError" in out.text


@seatbelt_only
async def test_cannot_detect_files_outside_scratch(secret_file):
    code = (
        "import os\n"
        f"print('exists', os.path.exists({secret_file!r}))\n"
        f"print('isfile', os.path.isfile({secret_file!r}))"
    )
    out = await _engine().execute(code, {})
    assert out.ok, out.text
    assert "exists False" in out.text
    assert "isfile False" in out.text


@seatbelt_only
async def test_common_stdlib_works_in_sandbox():
    code = (
        "import json, math, statistics, datetime, zoneinfo, re, collections, itertools\n"
        "import decimal, fractions, random, hashlib, csv, io, textwrap, os, pathlib, tempfile\n"
        "ny = datetime.datetime.now(zoneinfo.ZoneInfo('America/New_York'))\n"
        "with tempfile.NamedTemporaryFile('w', delete=False) as f: f.write('a,b\\n1,2\\n')\n"
        "rows = list(csv.reader(open(f.name)))\n"
        "print(statistics.mean([1, 2, 3]), ny.tzname() in ('EST', 'EDT'), rows[1], pathlib.Path.cwd().name[:8])"
    )
    out = await _engine().execute(code, {})
    assert out.ok, out.text
    assert "2 True ['1', '2'] ptc-run-" in out.text
