"""Tests for configuration loading and validation."""

import os
import tempfile

import pytest
import yaml

from ptc_mcp.config import Config, ExecutionConfig, ServerConfig, ToolsConfig, load_config


def _write_yaml(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(data))
    return path


class TestLoadConfig:
    def test_empty_file(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("")
        config = load_config(path)
        assert config.servers == []
        assert config.tools.allow == []
        assert config.tools.block == []
        assert config.execution.timeout_seconds == 120
        assert config.execution.max_output_bytes == 65536

    def test_defaults(self, tmp_path):
        path = _write_yaml(tmp_path, {"servers": []})
        config = load_config(path)
        assert config.execution.timeout_seconds == 120
        assert config.execution.max_output_bytes == 65536

    def test_stdio_server(self, tmp_path):
        data = {
            "servers": [
                {
                    "name": "test-server",
                    "transport": "stdio",
                    "command": "python",
                    "args": ["-m", "test_mod"],
                }
            ]
        }
        config = load_config(_write_yaml(tmp_path, data))
        assert len(config.servers) == 1
        srv = config.servers[0]
        assert srv.name == "test-server"
        assert srv.transport == "stdio"
        assert srv.command == "python"
        assert srv.args == ["-m", "test_mod"]

    def test_sse_server(self, tmp_path):
        data = {
            "servers": [
                {
                    "name": "sse-srv",
                    "transport": "sse",
                    "url": "http://localhost:8080/mcp",
                }
            ]
        }
        config = load_config(_write_yaml(tmp_path, data))
        assert config.servers[0].url == "http://localhost:8080/mcp"

    def test_stdio_missing_command(self, tmp_path):
        data = {"servers": [{"name": "bad", "transport": "stdio"}]}
        with pytest.raises(ValueError, match="stdio transport requires 'command'"):
            load_config(_write_yaml(tmp_path, data))

    def test_sse_missing_url(self, tmp_path):
        data = {"servers": [{"name": "bad", "transport": "sse"}]}
        with pytest.raises(ValueError, match="sse transport requires 'url'"):
            load_config(_write_yaml(tmp_path, data))

    def test_allow_and_block_mutually_exclusive(self, tmp_path):
        data = {
            "tools": {
                "allow": ["mcp__a__b"],
                "block": ["mcp__c__d"],
            }
        }
        with pytest.raises(ValueError, match="mutually exclusive"):
            load_config(_write_yaml(tmp_path, data))

    def test_block_list(self, tmp_path):
        data = {"tools": {"block": ["mcp__srv__dangerous_tool"]}}
        config = load_config(_write_yaml(tmp_path, data))
        assert "mcp__srv__dangerous_tool" in config.tools.block

    def test_allow_list(self, tmp_path):
        data = {"tools": {"allow": ["mcp__srv__safe_tool"]}}
        config = load_config(_write_yaml(tmp_path, data))
        assert "mcp__srv__safe_tool" in config.tools.allow

    def test_custom_execution(self, tmp_path):
        data = {"execution": {"timeout_seconds": 60, "max_output_bytes": 1024}}
        config = load_config(_write_yaml(tmp_path, data))
        assert config.execution.timeout_seconds == 60
        assert config.execution.max_output_bytes == 1024

    def test_server_with_env(self, tmp_path):
        data = {
            "servers": [
                {
                    "name": "env-srv",
                    "transport": "stdio",
                    "command": "node",
                    "args": ["server.js"],
                    "env": {"API_KEY": "test123"},
                }
            ]
        }
        config = load_config(_write_yaml(tmp_path, data))
        assert config.servers[0].env == {"API_KEY": "test123"}
