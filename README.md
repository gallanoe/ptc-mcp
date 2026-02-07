# PTC-MCP

Programmatic Tool Calling for Claude Code via MCP.

Claude Code on subscription plans lacks the Anthropic API's programmatic tool calling (PTC) feature, where Claude can write Python scripts that call multiple tools in a single execution. Without it, every tool invocation is a full model round-trip — intermediate results enter the context window, consuming tokens and adding latency.

PTC-MCP fixes this. It's an MCP server that exposes a single `execute_program` tool. Claude writes a Python script, the server runs it with MCP tools injected as async functions, and only stdout comes back. Intermediate tool results stay in the Python runtime and never enter the conversation.

## How it works

```
Claude Code
│
│  Writes a Python script using namespaced tool names.
│  Calls execute_program(code="...")
│
▼
PTC-MCP Server
│
├── Tool Registry
│     Connects to configured MCP servers at startup.
│     Fetches their tools, applies allow/block filters,
│     and registers each as a namespaced async function.
│
├── Execution Engine
│     Wraps the script in an async function, injects
│     the tool namespace, captures stdout, enforces
│     timeout and output size limits.
│
└── Returns stdout with a status prefix.
     Only this enters Claude Code's context.
```

At startup, PTC-MCP connects to your configured MCP servers as a client, discovers their tools, and makes them callable as `mcp__<server>__<tool>()` async functions inside scripts. When Claude calls `execute_program`, the script runs in-process with those functions available. Tool calls proxy to the real MCP servers, results stay local, and only `print()` output goes back.

## Example

Claude decides comparing three tickers benefits from batched execution:

```python
execute_program(code="""
tickers = ["AMZN", "MSFT", "GOOG"]
for t in tickers:
    data = await mcp__financial_data__query_financials(
        ticker=t, statement="income", period="quarter", limit=4
    )
    revenues = [q["revenue"] for q in data]
    trend = " → ".join(f"${r/1e9:.1f}B" for r in revenues)
    print(f"{t}: {trend}")
""")
```

Three tool calls happen inside the script. Claude sees only:

```
[Script executed successfully]
AMZN: $170.0B → $165.3B → $158.9B → $149.2B
MSFT: $65.6B → $62.0B → $59.1B → $56.5B
GOOG: $96.5B → $88.3B → $85.0B → $80.5B
```

## Setup

Requires Python 3.11+.

```bash
uv venv && uv pip install -e ".[dev]"
```

## Configuration

Create a `config.yaml` (or set `PTC_MCP_CONFIG` to point elsewhere):

```yaml
servers:
  - name: financial-data
    transport: stdio
    command: node
    args: ["./financial-data-mcp/dist/index.js"]

  - name: internal-apis
    transport: sse
    url: "http://localhost:8080/mcp"

tools:
  block:
    - "mcp__internal_apis__delete_resource"

execution:
  timeout_seconds: 120
  max_output_bytes: 65536
```

- **servers** — MCP servers to bridge. Supports `stdio` and `sse` transports.
- **tools.allow / tools.block** — Whitelist or blacklist namespaced tool names (mutually exclusive). Omit both to allow everything.
- **execution** — Timeout and output size limits for `execute_program`.

The server starts fine with no config file or an empty `servers` list.

## Running

```bash
# Directly
uv run python -m ptc_mcp

# Or via the installed entry point
ptc-mcp
```

The server communicates over stdio (JSON-RPC). Add it to your Claude Code MCP settings to use it.

## Testing

```bash
uv run pytest tests/ -v
```

Tests include unit tests for config parsing, the execution engine, registry filtering/namespacing, and end-to-end integration tests that spin up a real mock MCP server.
