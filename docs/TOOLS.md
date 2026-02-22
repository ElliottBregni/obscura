# Tool System

Obscura provides a tool framework for agents to execute actions: shell commands, Python code, file operations, and external service calls.

## @tool Decorator

Register tool handlers with the `@tool` decorator:

```python
from obscura.core.tools import tool

@tool(name="search_code", description="Search codebase for a pattern")
def search_code(pattern: str, file_type: str = "py") -> str:
    """Search for pattern in files of the given type."""
    import subprocess
    result = subprocess.run(
        ["grep", "-r", pattern, f"--include=*.{file_type}", "."],
        capture_output=True, text=True
    )
    return result.stdout
```

### Schema Inference

Type hints are automatically converted to JSON Schema:

| Python Type | JSON Schema |
|------------|-------------|
| `str` | `{"type": "string"}` |
| `int` | `{"type": "integer"}` |
| `float` | `{"type": "number"}` |
| `bool` | `{"type": "boolean"}` |
| Pydantic `BaseModel` | Full JSON Schema from model |

### Telemetry

Tool calls are automatically traced with OpenTelemetry spans and recorded as metrics (`tool_calls_total`, `tool_duration_seconds`). Gracefully degrades if telemetry is unavailable.

## Tool Policy Engine

First-class access control for tool invocations.

### ToolPolicy

```python
from obscura.tools.policy import ToolPolicy, evaluate_policy, PolicyResult

# Allow only specific tools
policy = ToolPolicy(
    name="restricted",
    allow_list=frozenset({"read_file", "search_files"}),
)

# Deny dangerous tools
policy = ToolPolicy(
    name="safe",
    deny_list=frozenset({"delete_file", "run_shell"}),
)

# Sandbox to a directory
policy = ToolPolicy(
    name="sandboxed",
    base_dir=Path("/home/user/project"),
)

# Full access (bypasses all checks)
policy = ToolPolicy(name="admin", full_access=True)
```

### Evaluation Order

1. `full_access` -- If True, allow everything
2. `deny_list` -- If tool name matches, deny
3. `allow_list` -- If non-empty, only listed tools are allowed
4. `base_dir` -- For filesystem tools, paths must stay within this directory

### PolicyResult

```python
result = evaluate_policy(policy, "read_file", {"path": "/etc/passwd"})
# PolicyResult(allowed=False, reason="path '/etc/passwd' escapes base_dir '/home/user/project'")

result = evaluate_policy(policy, "delete_file")
# PolicyResult(allowed=False, reason="tool 'delete_file' is in deny_list")
```

### Filesystem Tools

These tools are subject to `base_dir` path checking:

- `read_file`
- `write_file`
- `list_directory`
- `search_files`
- `create_directory`
- `delete_file`

Path resolution uses `Path.resolve()` to prevent symlink escapes.

## System Tools

Built-in shell and Python execution for agents.

### Security Controls

| Control | Default | Description |
|---------|---------|-------------|
| `OBSCURA_SYSTEM_TOOLS_UNSAFE_FULL_ACCESS` | `false` | Grant unrestricted access |
| Hardcoded deny list | `rm`, `sudo`, `shutdown`, `reboot`, `diskutil`, `mkfs`, `dd` | Always blocked |
| Allow/deny lists | Configurable via env | Additional restrictions |
| `base_dir` | Working directory | Filesystem sandbox |
| Timeout | 30s | Max execution time |

### Available Tools

| Tool | Tier | Description |
|------|------|-------------|
| Shell command execution | `operator` | Run shell commands (sandboxed) |
| `run_python3` | `privileged` | Execute Python code |

### Enabling System Tools

```bash
# Via API
curl -X POST http://localhost:8080/api/v1/agents \
  -d '{"name": "inspector", "model": "claude", "system_tools": {"enabled": true}}'
```

## Tool Providers

The `ToolProvider` protocol allows pluggable tool sources:

| Provider | Source | Description |
|----------|--------|-------------|
| `SystemToolProvider` | Built-in | Shell, Python execution |
| `MCPToolProvider` | MCP servers | Tools from external MCP servers |
| `A2ARemoteToolProvider` | A2A agents | Remote agent capabilities as tools |

```python
class ToolProvider(Protocol):
    async def install(self, context: ToolProviderContext) -> None: ...
    async def uninstall(self, context: ToolProviderContext) -> None: ...
```
