"""Tests for tool-result handling, budgets/trace/emit, config expansion, and
server reconnection."""

import asyncio
import sys
import time
from types import SimpleNamespace

import pytest
from mcp.types import TextContent

from ptc_mcp.config import Config, ExecutionConfig, ServerConfig, ToolsConfig, load_config
from ptc_mcp.errors import ToolError
from ptc_mcp.executor import ExecutionEngine
from ptc_mcp.registry import ToolRegistry


def _result(*texts, structured=None, is_error=False):
    return SimpleNamespace(
        content=[TextContent(type="text", text=t) for t in texts],
        structured_content=structured,
        is_error=is_error,
    )


def _engine(**kw) -> ExecutionEngine:
    return ExecutionEngine(ExecutionConfig(**{"timeout_seconds": 15, **kw}))


class TestParseResult:
    def test_is_error_raises_tool_error(self):
        with pytest.raises(ToolError, match="returned an error: Error: No options data"):
            ToolRegistry._parse_mcp_result(_result("Error: No options data", is_error=True), "t")

    def test_structured_preferred_and_notes_kept(self):
        # fmp style: a note first, then the JSON rendering of structuredContent
        r = _result("EMPTY RESULT — symbol may be invalid", '{"results": []}',
                    structured={"results": []})
        parsed = ToolRegistry._parse_mcp_result(r)
        assert parsed == {"results": [], "_notes": ["EMPTY RESULT — symbol may be invalid"]}

    def test_structured_without_notes_is_plain(self):
        r = _result('{"results": [1]}', structured={"results": [1]})
        assert ToolRegistry._parse_mcp_result(r) == {"results": [1]}

    def test_structured_with_prose_text(self):
        r = _result("Found 1 row.", structured={"rows": [1]})
        assert ToolRegistry._parse_mcp_result(r) == {"rows": [1], "_notes": ["Found 1 row."]}

    def test_fastmcp_wrapped_primitive_is_unwrapped(self):
        r = _result("Hello!", structured={"result": "Hello!"})
        assert ToolRegistry._parse_mcp_result(r) == "Hello!"

    def test_fastmcp_wrapped_json_string_is_decoded(self):
        r = _result('{"result": 7}', structured={"result": '{"result": 7}'})
        assert ToolRegistry._parse_mcp_result(r) == {"result": 7}

    def test_genuine_result_key_object_is_not_unwrapped(self):
        r = _result('{"result": 5}', structured={"result": 5})
        assert ToolRegistry._parse_mcp_result(r) == {"result": 5}

    def test_text_only_note_plus_json(self):
        r = _result("TRUNCATED — 5 rows", "[1, 2]")
        assert ToolRegistry._parse_mcp_result(r) == {"data": [1, 2], "_notes": ["TRUNCATED — 5 rows"]}


class TestGlobFilters:
    def _reg(self, **tools):
        return ToolRegistry(Config(tools=ToolsConfig(**tools)))

    def test_allow_glob(self):
        reg = self._reg(allow=["mcp__fmp__*", "mcp__massive_options__options_iv"])
        assert reg._is_allowed("mcp__fmp__stock_quote")
        assert reg._is_allowed("mcp__massive_options__options_iv")
        assert not reg._is_allowed("mcp__massive_options__options_flow")
        assert not reg._is_allowed("mcp__alpaca__CreateOrder")

    def test_block_glob(self):
        reg = self._reg(block=["mcp__*__delete_*"])
        assert not reg._is_allowed("mcp__srv__delete_all")
        assert reg._is_allowed("mcp__srv__read")


class TestConfigExpansion:
    def _load(self, tmp_path, text):
        p = tmp_path / "config.yaml"
        p.write_text(text)
        return load_config(p)

    def test_env_vars_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FMP_API_KEY", "k-123")
        monkeypatch.setenv("NODE_BIN", "/opt/node")
        cfg = self._load(tmp_path, """
servers:
  - name: fmp
    command: ${NODE_BIN}
    args: ["${HOME_DIR:-/home/x}/fmp/dist/index.js"]
    env:
      FMP_API_KEY: ${FMP_API_KEY}
""")
        s = cfg.servers[0]
        assert s.command == "/opt/node"
        assert s.args == ["/home/x/fmp/dist/index.js"]
        assert s.env == {"FMP_API_KEY": "k-123"}

    def test_unset_env_var_is_an_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PTC_TEST_MISSING", raising=False)
        with pytest.raises(ValueError, match=r"\$\{PTC_TEST_MISSING\} is not set"):
            self._load(tmp_path, """
servers:
  - name: fmp
    command: node
    env: {KEY: "${PTC_TEST_MISSING}"}
""")

    def test_http_transport_and_headers(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOKEN", "t0k")
        cfg = self._load(tmp_path, """
servers:
  - name: remote
    transport: http
    url: https://example.com/mcp
    headers: {Authorization: "Bearer ${TOKEN}"}
""")
        assert cfg.servers[0].transport == "http"
        assert cfg.servers[0].headers == {"Authorization": "Bearer t0k"}

    def test_http_requires_url(self, tmp_path):
        with pytest.raises(ValueError, match="http transport requires 'url'"):
            self._load(tmp_path, "servers:\n  - name: r\n    transport: http\n")

    def test_unknown_transport(self, tmp_path):
        with pytest.raises(ValueError, match="unknown transport"):
            self._load(tmp_path, "servers:\n  - name: r\n    transport: grpc\n    url: x\n")

    def test_duplicate_server_names(self, tmp_path):
        with pytest.raises(ValueError, match="duplicate server names"):
            self._load(tmp_path, "servers:\n  - {name: a, command: x}\n  - {name: a, command: y}\n")

    def test_budget_validation(self, tmp_path):
        with pytest.raises(ValueError, match="max_tool_calls must be > 0"):
            self._load(tmp_path, "execution: {max_tool_calls: 0}\n")
        with pytest.raises(ValueError, match="execution.trace"):
            self._load(tmp_path, "execution: {trace: verbose}\n")


class TestBudgetsAndTrace:
    async def test_budget_exhausted(self):
        async def tool(**kw):
            return 1

        code = (
            "ok = fails = 0\n"
            "for _ in range(5):\n"
            "    try:\n"
            "        await t()\n"
            "        ok += 1\n"
            "    except ToolError as e:\n"
            "        fails += 1\n"
            "        last = str(e)\n"
            "print(ok, fails, last)"
        )
        out = await _engine(max_tool_calls=3).execute(code, {"t": tool})
        assert out.ok, out.text
        assert "3 2 tool-call budget exhausted (max_tool_calls=3)" in out.text

    async def test_introspection_not_counted(self):
        async def tool(**kw):
            return 1

        async def list_callable_tools():
            return ["t"]

        code = "for _ in range(5):\n    await list_callable_tools()\nawait t()\nprint('fine')"
        out = await _engine(max_tool_calls=1).execute(
            code, {"t": tool, "list_callable_tools": list_callable_tools}
        )
        assert out.ok, out.text
        assert "[tool calls: 1;" in out.text

    async def test_concurrency_cap(self):
        live = {"now": 0, "peak": 0}

        async def slow(**kw):
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            await asyncio.sleep(0.1)
            live["now"] -= 1
            return 0

        code = "import asyncio\nawait asyncio.gather(*(s() for _ in range(12)))\nprint('done')"
        out = await _engine(max_concurrent_calls=3).execute(code, {"s": slow})
        assert out.ok, out.text
        assert live["peak"] == 3

    async def test_per_call_timeout(self):
        async def hang(**kw):
            await asyncio.sleep(30)

        code = "try:\n    await h()\nexcept ToolError as e:\n    print('caught', e)"
        start = time.monotonic()
        out = await _engine(tool_call_timeout_seconds=0.5).execute(code, {"h": hang})
        assert out.ok, out.text
        assert "timed out after 0.5s" in out.text
        assert time.monotonic() - start < 5

    async def test_trace_summary_and_structured_calls(self):
        async def good(**kw):
            return kw

        async def bad(**kw):
            raise ToolError("upstream 500")

        code = (
            "await good(symbol='AAPL')\n"
            "try:\n    await bad(symbol='X')\nexcept ToolError:\n    pass\n"
            "print('ok')"
        )
        out = await _engine().execute(code, {"good": good, "bad": bad})
        assert out.ok, out.text
        assert "[tool calls: 2, 1 failed;" in out.text
        assert "failed: bad: upstream 500" in out.text
        calls = out.structured["tool_calls"]
        assert [c["tool"] for c in sorted(calls, key=lambda c: c["tool"])] == ["bad", "good"]
        good_call = next(c for c in calls if c["tool"] == "good")
        assert good_call["ok"] is True and good_call["args"] == '{"symbol": "AAPL"}'

    async def test_trace_off(self):
        async def good(**kw):
            return 1

        out = await _engine(trace="off").execute("await good()\nprint('x')", {"good": good})
        assert "[tool calls" not in out.text
        assert len(out.structured["tool_calls"]) == 1


class TestEmit:
    async def test_emit_structured_result(self):
        out = await _engine().execute("emit({'best': 'AAPL', 'score': 0.9})\nprint('done')", {})
        assert out.ok, out.text
        assert out.structured["result"] == {"best": "AAPL", "score": 0.9}
        assert '--- result ---\n{"best": "AAPL", "score": 0.9}' in out.text

    async def test_last_emit_wins(self):
        out = await _engine().execute("emit(1)\nemit([2, 3])", {})
        assert out.structured["result"] == [2, 3]

    async def test_no_emit_means_null_result(self):
        out = await _engine().execute("print('hi')", {})
        assert out.structured["result"] is None
        assert "--- result ---" not in out.text

    async def test_emit_rejects_non_json(self):
        out = await _engine().execute("emit({1, 2})", {})
        assert not out.ok
        assert "JSON-serializable" in out.text

    async def test_emit_respects_output_limit(self):
        out = await _engine(max_output_bytes=100).execute("emit('x' * 500)", {})
        assert not out.ok
        assert "the limit is 100" in out.text

    async def test_failure_structured(self):
        out = await _engine().execute("1/0", {})
        assert out.structured["ok"] is False
        assert "ZeroDivisionError" in out.structured["error"]


def _mock_server(name="mock-test") -> ServerConfig:
    return ServerConfig(name=name, transport="stdio", command=sys.executable,
                        args=["-m", "tests.mock_server"])


async def _with_registry(config, fn):
    reg = ToolRegistry(config)
    await reg.initialize()
    try:
        await fn(reg)
    finally:
        await reg.shutdown()


class TestServerLifecycle:
    async def test_tool_is_error_becomes_tool_error(self):
        async def check(reg):
            with pytest.raises(ToolError, match="returned an error: .*mock failure"):
                await reg.get_namespace()["mcp__mock_test__fail"]()

        await _with_registry(Config(servers=[_mock_server()]), check)

    async def test_unexpected_server_exception_is_tool_error_without_reconnect(self):
        async def check(reg):
            ns = reg.get_namespace()
            with pytest.raises(ToolError, match="mcp__mock_test__boom' returned an error: Error executing tool boom"):
                await ns["mcp__mock_test__boom"]()
            assert reg.server_status()["mock-test"]["connected"] is True
            assert await ns["mcp__mock_test__add"](a=1, b=1) == {"result": 2}

        await _with_registry(Config(servers=[_mock_server()]), check)

    async def test_failed_server_is_reported(self):
        bad = ServerConfig(name="broken", transport="stdio", command="/nonexistent/bin/server")

        async def check(reg):
            status = reg.server_status()
            assert status["broken"]["connected"] is False
            assert status["broken"]["error"]
            assert status["mock-test"]["connected"] is True
            assert "broken" in reg.unavailable_servers()
            assert any(n.startswith("mcp__mock_test__") for n in reg.get_namespace())

        await _with_registry(Config(servers=[_mock_server(), bad]), check)

    async def test_reconnects_after_server_dies(self):
        async def check(reg):
            ns = reg.get_namespace()
            with pytest.raises(ToolError):
                await ns["mcp__mock_test__crash"]()
            assert reg.server_status()["mock-test"]["connected"] is False
            deadline = time.monotonic() + 15
            while not reg.server_status()["mock-test"]["connected"]:
                assert time.monotonic() < deadline, "did not reconnect"
                await asyncio.sleep(0.2)
            # the handler object from before the crash works again
            assert await ns["mcp__mock_test__add"](a=2, b=2) == {"result": 4}

        await _with_registry(Config(servers=[_mock_server()]), check)

    async def test_script_can_check_server_status(self):
        engine = _engine()

        async def check(reg):
            out = await engine.execute(
                "s = await server_status()\nprint(s['mock-test']['connected'])",
                reg.get_namespace(),
            )
            assert out.ok, out.text
            assert "True" in out.text

        await _with_registry(Config(servers=[_mock_server()]), check)


class _FakeConn:
    def __init__(self, exc):
        self.name = "srv"
        self.error = None
        self.reconnects = []

        async def call_tool(name, args):
            raise exc

        self.session = SimpleNamespace(call_tool=call_tool)

    def request_reconnect(self, reason):
        self.reconnects.append(reason)


class TestUnexpectedResults:
    def _handler(self, exc):
        conn = _FakeConn(exc)
        reg = ToolRegistry(Config())
        return reg._make_bridge_handler(conn, "tool", "mcp__srv__tool"), conn

    async def test_schema_mismatch_is_tool_error_without_reconnect(self):
        handler, conn = self._handler(RuntimeError(
            "Invalid structured content returned by tool tool: None is not of type 'number'"))
        with pytest.raises(ToolError, match="does not match its declared output schema"):
            await handler()
        assert conn.reconnects == []

    async def test_missing_structured_content_is_schema_mismatch(self):
        handler, conn = self._handler(RuntimeError(
            "Tool tool has an output schema but did not return structured content"))
        with pytest.raises(ToolError, match="declared output schema"):
            await handler()
        assert conn.reconnects == []

    async def test_transport_failure_reconnects(self):
        import anyio

        handler, conn = self._handler(anyio.ClosedResourceError())
        with pytest.raises(ToolError, match="connection to 'srv' lost"):
            await handler()
        assert len(conn.reconnects) == 1

    async def test_other_errors_do_not_reconnect(self):
        handler, conn = self._handler(ValueError("weird payload"))
        with pytest.raises(ToolError, match="ValueError: weird payload"):
            await handler()
        assert conn.reconnects == []

    def test_non_text_content_leaves_a_placeholder(self):
        from mcp.types import ImageContent

        img = ImageContent(type="image", data="aGk=", mime_type="image/png")
        only_image = SimpleNamespace(content=[img], structured_content=None, is_error=False)
        assert ToolRegistry._parse_mcp_result(only_image) == {
            "data": None, "_notes": ["[non-text content omitted: image (image/png)]"]}
        mixed = SimpleNamespace(content=[TextContent(type="text", text='{"a": 1}'), img],
                                structured_content=None, is_error=False)
        assert ToolRegistry._parse_mcp_result(mixed) == {
            "a": 1, "_notes": ["[non-text content omitted: image (image/png)]"]}

    async def test_oversized_result_is_refused(self):
        async def big(**kw):
            return "x" * 5000

        code = "try:\n    await big()\nexcept ToolError as e:\n    print('refused:', e)"
        out = await _engine(max_tool_result_bytes=1000).execute(code, {"big": big})
        assert out.ok, out.text
        assert "over the 1000-byte limit" in out.text

    def test_result_cap_must_fit_the_channel(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text("execution: {max_tool_result_bytes: 100000000}\n")
        with pytest.raises(ValueError, match="max_tool_result_bytes must be <="):
            load_config(p)
