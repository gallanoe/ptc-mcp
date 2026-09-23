"""Integration tests: real ToolRegistry + mock_server + executor."""

import json
import sys

import pytest

from ptc_mcp.config import Config, ExecutionConfig, ServerConfig, ToolsConfig
from ptc_mcp.executor import ExecutionEngine
from ptc_mcp.registry import ToolRegistry


def _mock_server_config() -> ServerConfig:
    """Create a ServerConfig pointing to our mock FastMCP server."""
    return ServerConfig(
        name="mock-test",
        transport="stdio",
        command=sys.executable,
        args=["-m", "tests.mock_server"],
    )


def _make_config(**kwargs) -> Config:
    return Config(
        servers=[_mock_server_config()],
        execution=ExecutionConfig(timeout_seconds=10),
        **kwargs,
    )


async def _run_with_registry(config, callback):
    """Helper that creates a registry, runs a callback, and shuts down cleanly."""
    reg = ToolRegistry(config)
    await reg.initialize()
    try:
        await callback(reg)
    finally:
        try:
            await reg.shutdown()
        except RuntimeError:
            pass  # anyio cancel scope teardown across tasks


class TestIntegration:
    async def test_registry_discovers_tools(self):
        async def check(reg):
            ns = reg.get_namespace()
            assert "mcp__mock_test__add" in ns
            assert "mcp__mock_test__greet" in ns
            assert "mcp__mock_test__get_data" in ns

        await _run_with_registry(_make_config(), check)

    async def test_call_add_tool(self):
        async def check(reg):
            ns = reg.get_namespace()
            result = await ns["mcp__mock_test__add"](a=3, b=4)
            assert result == {"result": 7}

        await _run_with_registry(_make_config(), check)

    async def test_call_greet_tool(self):
        async def check(reg):
            ns = reg.get_namespace()
            result = await ns["mcp__mock_test__greet"](name="World")
            assert "Hello, World!" in str(result)

        await _run_with_registry(_make_config(), check)

    async def test_call_get_data_tool(self):
        async def check(reg):
            ns = reg.get_namespace()
            result = await ns["mcp__mock_test__get_data"](key="users")
            assert isinstance(result, list)
            assert result[0]["name"] == "Alice"

        await _run_with_registry(_make_config(), check)

    async def test_executor_with_bridged_tools(self):
        executor = ExecutionEngine(ExecutionConfig(timeout_seconds=10, max_output_bytes=65536))

        async def check(reg):
            code = (
                "result = await mcp__mock_test__add(a=10, b=20)\n"
                "print(f'Sum: {result[\"result\"]}')"
            )
            output = await executor.run(code, reg.get_namespace())
            assert "[Script executed successfully]" in output
            assert "Sum: 30" in output

        await _run_with_registry(_make_config(), check)

    async def test_executor_loop_over_tools(self):
        executor = ExecutionEngine(ExecutionConfig(timeout_seconds=10, max_output_bytes=65536))

        async def check(reg):
            code = (
                "for name in ['Alice', 'Bob']:\n"
                "    greeting = await mcp__mock_test__greet(name=name)\n"
                "    print(greeting)"
            )
            output = await executor.run(code, reg.get_namespace())
            assert "Hello, Alice!" in output
            assert "Hello, Bob!" in output

        await _run_with_registry(_make_config(), check)

    async def test_executor_tool_chain(self):
        executor = ExecutionEngine(ExecutionConfig(timeout_seconds=10, max_output_bytes=65536))

        async def check(reg):
            code = (
                "data = await mcp__mock_test__get_data(key='users')\n"
                "for user in data:\n"
                "    greeting = await mcp__mock_test__greet(name=user['name'])\n"
                "    print(greeting)"
            )
            output = await executor.run(code, reg.get_namespace())
            assert "Hello, Alice!" in output
            assert "Hello, Bob!" in output

        await _run_with_registry(_make_config(), check)


class TestIntegrationWithFilters:
    async def test_block_filter(self):
        config = _make_config(tools=ToolsConfig(block=["mcp__mock_test__add"]))

        async def check(reg):
            ns = reg.get_namespace()
            assert "mcp__mock_test__add" not in ns
            assert "mcp__mock_test__greet" in ns

        await _run_with_registry(config, check)

    async def test_allow_filter(self):
        config = _make_config(tools=ToolsConfig(allow=["mcp__mock_test__greet"]))

        async def check(reg):
            ns = reg.get_namespace()
            assert "mcp__mock_test__greet" in ns
            assert "mcp__mock_test__add" not in ns
            assert "mcp__mock_test__get_data" not in ns

        await _run_with_registry(config, check)


class TestInspectToolIntegration:
    async def test_inspect_known_tool(self):
        async def check(reg):
            result = json.loads(reg.inspect_tool("mcp__mock_test__add"))
            assert result["name"] == "mcp__mock_test__add"
            assert result["description"]  # non-empty
            assert "properties" in result["inputSchema"]
            # FastMCP generates an outputSchema from the return type annotation
            assert "outputSchema" in result

        await _run_with_registry(_make_config(), check)

    async def test_inspect_unknown_tool(self):
        async def check(reg):
            result = reg.inspect_tool("mcp__nonexistent__foo")
            assert result.startswith("[Tool not found]")

        await _run_with_registry(_make_config(), check)


class TestListCallableToolsIntegration:
    async def test_lists_discovered_tools(self):
        async def check(reg):
            result = json.loads(reg.list_tool_names())
            assert "mcp__mock_test__add" in result
            assert "mcp__mock_test__greet" in result
            assert "mcp__mock_test__get_data" in result
            # Verify sorted order
            assert result == sorted(result)

        await _run_with_registry(_make_config(), check)


class TestInScriptIntrospection:
    async def test_list_callable_tools_returns_list(self):
        executor = ExecutionEngine(ExecutionConfig(timeout_seconds=10, max_output_bytes=65536))

        async def check(reg):
            code = (
                "tools = await list_callable_tools()\n"
                "print(type(tools).__name__)\n"
                "print('add_present:', 'mcp__mock_test__add' in tools)\n"
                "print('is_sorted:', tools == sorted(tools))"
            )
            output = await executor.run(code, reg.get_namespace())
            assert "[Script executed successfully]" in output
            assert "list" in output
            assert "add_present: True" in output
            assert "is_sorted: True" in output

        await _run_with_registry(_make_config(), check)

    async def test_inspect_tool_returns_dict(self):
        executor = ExecutionEngine(ExecutionConfig(timeout_seconds=10, max_output_bytes=65536))

        async def check(reg):
            code = (
                "info = await inspect_tool(tool_name='mcp__mock_test__add')\n"
                "print(type(info).__name__)\n"
                "print('name:', info['name'])\n"
                "print('has_input:', 'inputSchema' in info)\n"
                "print('has_output:', 'outputSchema' in info)"
            )
            output = await executor.run(code, reg.get_namespace())
            assert "[Script executed successfully]" in output
            assert "dict" in output
            assert "name: mcp__mock_test__add" in output
            assert "has_input: True" in output
            assert "has_output: True" in output

        await _run_with_registry(_make_config(), check)

    async def test_inspect_unknown_tool_returns_string(self):
        executor = ExecutionEngine(ExecutionConfig(timeout_seconds=10, max_output_bytes=65536))

        async def check(reg):
            code = (
                "result = await inspect_tool(tool_name='mcp__nonexistent__foo')\n"
                "print(type(result).__name__)\n"
                "print(result)"
            )
            output = await executor.run(code, reg.get_namespace())
            assert "[Script executed successfully]" in output
            assert "str" in output
            assert "[Tool not found]" in output

        await _run_with_registry(_make_config(), check)

    async def test_list_inspect_call_workflow(self):
        executor = ExecutionEngine(ExecutionConfig(timeout_seconds=10, max_output_bytes=65536))

        async def check(reg):
            code = (
                "# Step 1: discover tools\n"
                "tools = await list_callable_tools()\n"
                "# Step 2: find and inspect the add tool\n"
                "add_name = [t for t in tools if 'add' in t][0]\n"
                "schema = await inspect_tool(tool_name=add_name)\n"
                "print('tool:', schema['name'])\n"
                "# Step 3: call it dynamically using the namespace\n"
                "result = await mcp__mock_test__add(a=5, b=7)\n"
                "print('result:', result)"
            )
            output = await executor.run(code, reg.get_namespace())
            assert "[Script executed successfully]" in output
            assert "tool: mcp__mock_test__add" in output
            assert "result:" in output
            assert "12" in output

        await _run_with_registry(_make_config(), check)
