"""YAML configuration loading and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TRANSPORTS = ("stdio", "sse", "http")
SANDBOX_MODES = ("seatbelt", "none")
TRACE_MODES = ("summary", "off")
# Must stay below the child runner's 64 MiB channel read limit (see _runner.py).
MAX_TOOL_RESULT_CEILING = 48 * 1024 * 1024


@dataclass
class ServerConfig:
    """Configuration for a downstream MCP server."""

    name: str
    transport: str  # "stdio", "sse", or "http" (streamable HTTP)
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ToolsConfig:
    """Tool-level access control. Entries are namespaced names or glob patterns
    (e.g. ``mcp__fmp__*``)."""

    allow: list[str] = field(default_factory=list)
    block: list[str] = field(default_factory=list)


@dataclass
class ExecutionConfig:
    """Runtime execution constraints."""

    timeout_seconds: int = 120
    max_output_bytes: int = 65536
    # "seatbelt": run scripts under sandbox-exec (macOS). "none": child process
    # without an OS sandbox (explicit opt-in, e.g. for Linux CI).
    sandbox: str = "seatbelt"
    # Per-program tool-call budget (introspection helpers are not counted).
    max_tool_calls: int = 100
    max_concurrent_calls: int = 8
    tool_call_timeout_seconds: float = 60
    # Largest single tool result passed into a script (serialized JSON bytes).
    max_tool_result_bytes: int = 16 * 1024 * 1024
    # "summary": append a one-line call count (plus any failures) to the output.
    trace: str = "summary"


@dataclass
class Config:
    """Top-level configuration."""

    servers: list[ServerConfig] = field(default_factory=list)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any, where: str) -> Any:
    """Replace ``${VAR}`` / ``${VAR:-default}`` in strings (recursively).

    Lets secrets live in the environment instead of the config file. An unset
    variable without a default is a config error, never an empty string.
    """
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ValueError(f"{where}: environment variable ${{{name}}} is not set")

        return _ENV_REF.sub(sub, value)
    if isinstance(value, list):
        return [expand_env(v, where) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v, where) for k, v in value.items()}
    return value


def load_config(path: str | Path) -> Config:
    """Load and validate configuration from a YAML file."""
    path = Path(path)
    with open(path) as f:
        raw = yaml.safe_load(f)

    if raw is None:
        return Config()

    servers = []
    for entry in raw.get("servers", []) or []:
        name = entry["name"]
        where = f"Server '{name}'"
        transport = entry.get("transport", "stdio")
        if transport not in TRANSPORTS:
            raise ValueError(f"{where}: unknown transport {transport!r} (expected one of {TRANSPORTS})")
        sc = ServerConfig(
            name=name,
            transport=transport,
            command=expand_env(entry.get("command"), where),
            args=[str(a) for a in expand_env(entry.get("args", []) or [], where)],
            env={k: str(v) for k, v in expand_env(entry.get("env", {}) or {}, where).items()},
            url=expand_env(entry.get("url"), where),
            headers={k: str(v) for k, v in expand_env(entry.get("headers", {}) or {}, where).items()},
        )
        if transport == "stdio" and not sc.command:
            raise ValueError(f"{where}: stdio transport requires 'command'")
        if transport in ("sse", "http") and not sc.url:
            raise ValueError(f"{where}: {transport} transport requires 'url'")
        servers.append(sc)

    names = [s.name for s in servers]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"duplicate server names: {dupes}")

    tools_raw = raw.get("tools", {}) or {}
    tools = ToolsConfig(
        allow=tools_raw.get("allow", []) or [],
        block=tools_raw.get("block", []) or [],
    )
    if tools.allow and tools.block:
        raise ValueError("'allow' and 'block' are mutually exclusive in tools config")

    exec_raw = raw.get("execution", {}) or {}
    defaults = ExecutionConfig()
    execution = ExecutionConfig(
        timeout_seconds=exec_raw.get("timeout_seconds", defaults.timeout_seconds),
        max_output_bytes=exec_raw.get("max_output_bytes", defaults.max_output_bytes),
        sandbox=exec_raw.get("sandbox", defaults.sandbox),
        max_tool_calls=exec_raw.get("max_tool_calls", defaults.max_tool_calls),
        max_concurrent_calls=exec_raw.get("max_concurrent_calls", defaults.max_concurrent_calls),
        tool_call_timeout_seconds=exec_raw.get(
            "tool_call_timeout_seconds", defaults.tool_call_timeout_seconds
        ),
        trace=exec_raw.get("trace", defaults.trace),
        max_tool_result_bytes=exec_raw.get("max_tool_result_bytes", defaults.max_tool_result_bytes),
    )
    if execution.sandbox not in SANDBOX_MODES:
        raise ValueError(
            f"execution.sandbox must be 'seatbelt' or 'none', got {execution.sandbox!r}"
        )
    if execution.trace not in TRACE_MODES:
        raise ValueError(f"execution.trace must be one of {TRACE_MODES}, got {execution.trace!r}")
    for key in ("max_tool_calls", "max_concurrent_calls", "tool_call_timeout_seconds",
                "max_tool_result_bytes"):
        if not getattr(execution, key) > 0:
            raise ValueError(f"execution.{key} must be > 0")
    if execution.max_tool_result_bytes > MAX_TOOL_RESULT_CEILING:
        raise ValueError(
            f"execution.max_tool_result_bytes must be <= {MAX_TOOL_RESULT_CEILING} "
            "(the script's channel limit)"
        )

    return Config(servers=servers, tools=tools, execution=execution)
