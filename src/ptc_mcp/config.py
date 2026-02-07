"""YAML configuration loading and validation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ServerConfig:
    """Configuration for a downstream MCP server."""

    name: str
    transport: str  # "stdio" or "sse"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None


@dataclass
class ToolsConfig:
    """Tool-level access control."""

    allow: list[str] = field(default_factory=list)
    block: list[str] = field(default_factory=list)


@dataclass
class ExecutionConfig:
    """Runtime execution constraints."""

    timeout_seconds: int = 120
    max_output_bytes: int = 65536


@dataclass
class Config:
    """Top-level configuration."""

    servers: list[ServerConfig] = field(default_factory=list)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)


def load_config(path: str | Path) -> Config:
    """Load and validate configuration from a YAML file."""
    path = Path(path)
    with open(path) as f:
        raw = yaml.safe_load(f)

    if raw is None:
        return Config()

    servers = []
    for entry in raw.get("servers", []) or []:
        transport = entry.get("transport", "stdio")
        sc = ServerConfig(
            name=entry["name"],
            transport=transport,
            command=entry.get("command"),
            args=entry.get("args", []),
            env=entry.get("env", {}),
            url=entry.get("url"),
        )
        if transport == "stdio" and not sc.command:
            raise ValueError(
                f"Server '{sc.name}': stdio transport requires 'command'"
            )
        if transport == "sse" and not sc.url:
            raise ValueError(
                f"Server '{sc.name}': sse transport requires 'url'"
            )
        servers.append(sc)

    tools_raw = raw.get("tools", {}) or {}
    tools = ToolsConfig(
        allow=tools_raw.get("allow", []) or [],
        block=tools_raw.get("block", []) or [],
    )
    if tools.allow and tools.block:
        raise ValueError("'allow' and 'block' are mutually exclusive in tools config")

    exec_raw = raw.get("execution", {}) or {}
    execution = ExecutionConfig(
        timeout_seconds=exec_raw.get("timeout_seconds", 120),
        max_output_bytes=exec_raw.get("max_output_bytes", 65536),
    )

    return Config(servers=servers, tools=tools, execution=execution)
