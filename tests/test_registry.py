"""Tests for the tool registry (unit tests on static/pure methods)."""

import json
from unittest.mock import MagicMock

import pytest

from ptc_mcp.config import Config, ToolsConfig
from ptc_mcp.registry import RegisteredTool, ToolRegistry


class TestNamespacing:
    def test_basic(self):
        result = ToolRegistry._make_namespaced_name("financial-data", "query")
        assert result == "mcp__financial_data__query"

    def test_underscores_preserved(self):
        result = ToolRegistry._make_namespaced_name("my_server", "my_tool")
        assert result == "mcp__my_server__my_tool"

    def test_multiple_hyphens(self):
        result = ToolRegistry._make_namespaced_name("my-cool-server", "get-data-now")
        assert result == "mcp__my_cool_server__get_data_now"

    def test_no_hyphens(self):
        result = ToolRegistry._make_namespaced_name("server", "tool")
        assert result == "mcp__server__tool"


class TestAllowBlockFiltering:
    def test_no_filters_allows_all(self):
        config = Config(tools=ToolsConfig())
        reg = ToolRegistry(config)
        assert reg._is_allowed("mcp__any__tool") is True

    def test_allow_list_permits_listed(self):
        config = Config(tools=ToolsConfig(allow=["mcp__srv__ok"]))
        reg = ToolRegistry(config)
        assert reg._is_allowed("mcp__srv__ok") is True

    def test_allow_list_blocks_unlisted(self):
        config = Config(tools=ToolsConfig(allow=["mcp__srv__ok"]))
        reg = ToolRegistry(config)
        assert reg._is_allowed("mcp__srv__other") is False

    def test_block_list_blocks_listed(self):
        config = Config(tools=ToolsConfig(block=["mcp__srv__bad"]))
        reg = ToolRegistry(config)
        assert reg._is_allowed("mcp__srv__bad") is False

    def test_block_list_allows_unlisted(self):
        config = Config(tools=ToolsConfig(block=["mcp__srv__bad"]))
        reg = ToolRegistry(config)
        assert reg._is_allowed("mcp__srv__good") is True


class TestParseResult:
    def _make_text_content(self, text):
        from mcp.types import TextContent

        return TextContent(type="text", text=text)

    def test_json_string(self):
        result = MagicMock()
        result.content = [self._make_text_content('{"key": "value"}')]
        parsed = ToolRegistry._parse_mcp_result(result)
        assert parsed == {"key": "value"}

    def test_json_array(self):
        result = MagicMock()
        result.content = [self._make_text_content("[1, 2, 3]")]
        parsed = ToolRegistry._parse_mcp_result(result)
        assert parsed == [1, 2, 3]

    def test_plain_string(self):
        result = MagicMock()
        result.content = [self._make_text_content("Hello, World!")]
        parsed = ToolRegistry._parse_mcp_result(result)
        assert parsed == "Hello, World!"

    def test_empty_content(self):
        result = MagicMock()
        result.content = []
        parsed = ToolRegistry._parse_mcp_result(result)
        assert parsed is None

    def test_multiple_text_contents(self):
        result = MagicMock()
        result.content = [
            self._make_text_content("line1"),
            self._make_text_content("line2"),
        ]
        parsed = ToolRegistry._parse_mcp_result(result)
        assert parsed == "line1\nline2"


class TestListToolNames:
    def _make_registry(self) -> ToolRegistry:
        config = Config(tools=ToolsConfig())
        return ToolRegistry(config)

    def test_empty_registry(self):
        reg = self._make_registry()
        result = json.loads(reg.list_tool_names())
        assert result == []

    def test_with_tools(self):
        reg = self._make_registry()
        reg._tools["mcp__srv__beta"] = RegisteredTool(
            name="mcp__srv__beta",
            description="Beta tool",
            parameters={"type": "object", "properties": {}},
            output_schema=None,
            handler=lambda **kw: None,
        )
        reg._tools["mcp__srv__alpha"] = RegisteredTool(
            name="mcp__srv__alpha",
            description="Alpha tool",
            parameters={"type": "object", "properties": {}},
            output_schema=None,
            handler=lambda **kw: None,
        )
        result = json.loads(reg.list_tool_names())
        assert result == ["mcp__srv__alpha", "mcp__srv__beta"]


class TestInspectTool:
    def _make_registry(self) -> ToolRegistry:
        config = Config(tools=ToolsConfig())
        return ToolRegistry(config)

    def test_tool_found_without_output_schema(self):
        reg = self._make_registry()
        reg._tools["mcp__srv__mytool"] = RegisteredTool(
            name="mcp__srv__mytool",
            description="A test tool",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "required": ["x"],
            },
            output_schema=None,
            handler=lambda **kw: None,
        )
        result = json.loads(reg.inspect_tool("mcp__srv__mytool"))
        assert result["name"] == "mcp__srv__mytool"
        assert result["description"] == "A test tool"
        assert result["inputSchema"]["properties"]["x"]["type"] == "integer"
        assert result["outputSchema"] is None
        assert "note" in result

    def test_tool_found_with_output_schema(self):
        reg = self._make_registry()
        output_schema = {
            "type": "object",
            "properties": {"value": {"type": "number"}},
        }
        reg._tools["mcp__srv__mytool"] = RegisteredTool(
            name="mcp__srv__mytool",
            description="A tool with output schema",
            parameters={"type": "object", "properties": {}},
            output_schema=output_schema,
            handler=lambda **kw: None,
        )
        result = json.loads(reg.inspect_tool("mcp__srv__mytool"))
        assert result["outputSchema"] == output_schema
        assert "note" not in result

    def test_tool_not_found(self):
        reg = self._make_registry()
        result = reg.inspect_tool("mcp__nonexistent__foo")
        assert result.startswith("[Tool not found]")
