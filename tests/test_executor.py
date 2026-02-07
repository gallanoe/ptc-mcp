"""Tests for the execution engine."""

import pytest

from ptc_mcp.config import ExecutionConfig
from ptc_mcp.errors import ToolError
from ptc_mcp.executor import ExecutionEngine


@pytest.fixture
def engine():
    return ExecutionEngine(ExecutionConfig(timeout_seconds=5, max_output_bytes=1024))


class TestExecutionEngine:
    async def test_simple_print(self, engine):
        result = await engine.run("print('hello world')", {})
        assert "[Script executed successfully]" in result
        assert "hello world" in result

    async def test_no_output(self, engine):
        result = await engine.run("x = 1 + 1", {})
        assert "[Script executed successfully]" in result
        assert "(no output)" in result

    async def test_multiline(self, engine):
        code = "for i in range(3):\n    print(i)"
        result = await engine.run(code, {})
        assert "0" in result
        assert "1" in result
        assert "2" in result

    async def test_syntax_error(self, engine):
        result = await engine.run("def foo(", {})
        assert "[Script execution failed]" in result
        assert "SyntaxError" in result

    async def test_runtime_error(self, engine):
        result = await engine.run("x = 1 / 0", {})
        assert "[Script execution failed]" in result
        assert "ZeroDivisionError" in result

    async def test_name_error(self, engine):
        result = await engine.run("print(undefined_var)", {})
        assert "[Script execution failed]" in result
        assert "NameError" in result

    async def test_timeout(self):
        engine = ExecutionEngine(ExecutionConfig(timeout_seconds=1, max_output_bytes=1024))
        code = "import asyncio\nawait asyncio.sleep(10)"
        result = await engine.run(code, {})
        assert "[Script execution failed]" in result
        assert "TimeoutError" in result
        assert "1s limit" in result

    async def test_output_truncation(self):
        engine = ExecutionEngine(ExecutionConfig(timeout_seconds=5, max_output_bytes=50))
        code = "print('x' * 200)"
        result = await engine.run(code, {})
        assert "... (truncated)" in result

    async def test_async_tool_call(self, engine):
        async def mock_tool(**kwargs):
            return {"result": kwargs["a"] + kwargs["b"]}

        namespace = {"add": mock_tool}
        code = "result = await add(a=2, b=3)\nprint(result)"
        result = await engine.run(code, namespace)
        assert "[Script executed successfully]" in result
        assert "{'result': 5}" in result

    async def test_tool_error_propagation(self, engine):
        async def failing_tool(**kwargs):
            raise ToolError("'mcp__test__fail' failed: connection refused")

        namespace = {"mcp__test__fail": failing_tool}
        code = "await mcp__test__fail()"
        result = await engine.run(code, {})
        assert "[Script execution failed]" in result
        assert "NameError" in result

    async def test_tool_error_in_namespace(self, engine):
        async def failing_tool(**kwargs):
            raise ToolError("'mcp__test__fail' failed: connection refused")

        namespace = {"mcp__test__fail": failing_tool}
        code = "await mcp__test__fail()"
        result = await engine.run(code, namespace)
        assert "[Script execution failed]" in result
        assert "ToolError" in result

    async def test_multiple_tool_calls(self, engine):
        call_count = 0

        async def counter_tool(**kwargs):
            nonlocal call_count
            call_count += 1
            return call_count

        namespace = {"get_count": counter_tool}
        code = (
            "a = await get_count()\n"
            "b = await get_count()\n"
            "c = await get_count()\n"
            "print(f'{a} {b} {c}')"
        )
        result = await engine.run(code, namespace)
        assert "1 2 3" in result

    async def test_fresh_namespace_per_run(self, engine):
        code1 = "x = 42\nprint(x)"
        result1 = await engine.run(code1, {})
        assert "42" in result1

        code2 = "print(x)"
        result2 = await engine.run(code2, {})
        assert "[Script execution failed]" in result2
        assert "NameError" in result2
