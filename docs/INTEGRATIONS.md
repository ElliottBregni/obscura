# Integrations

Obscura integrates with two protocols for extending agent capabilities: MCP (Model Context Protocol) and A2A (Agent-to-Agent).

## MCP (Model Context Protocol)

Full MCP client and server implementation with stdio and SSE transports.

### MCP Client

Connect to external MCP servers to access their tools:

```python
from obscura.integrations.mcp.client import MCPClient
from obscura.integrations.mcp.types import MCPConnectionConfig, MCPTransportType

# Connect via stdio
config = MCPConnectionConfig(
    transport=MCPTransportType.STDIO,
    command="npx",
    args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
)

async with MCPClient(config) as client:
    tools = await client.list_tools()
    result = await client.call_tool("read_file", {"path": "/tmp/test.txt"})
```

### MCP Server

Obscura exposes its tools as an MCP server:

| Endpoint | Description |
|----------|-------------|
| `POST /mcp/tools/list` | List available tools |
| `POST /mcp/tools/call` | Execute a tool |
| `POST /mcp/resources/list` | List resources |
| `POST /mcp/prompts/list` | List prompts |

### MCP Configuration

Configure MCP servers in `~/.obscura/mcp-config.json` or via the agent spawn API:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user"]
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "..."}
    }
  }
}
```

### MCP with Agents

```bash
curl -X POST http://localhost:8080/api/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "fs-agent",
    "model": "claude",
    "mcp": {
      "enabled": true,
      "config_path": "~/.obscura/mcp-config.json",
      "server_names": ["filesystem"],
      "auto_discover": true
    }
  }'
```

### Tool Conversion

Bidirectional conversion between Obscura `ToolSpec` and MCP `Tool`:

```python
from obscura.integrations.mcp.tools import obscura_tool_to_mcp, mcp_tool_to_obscura

# Obscura -> MCP
mcp_tool = obscura_tool_to_mcp(tool_spec)

# MCP -> Obscura (wraps in async handler)
tool_spec = mcp_tool_to_obscura(mcp_tool, client)
```

### Features

- JSON-RPC 2.0 with request/response correlation
- Request timeout enforcement
- Async-first design
- Supports stdio and SSE transports
- Tool, resource, and prompt discovery

## A2A (Agent-to-Agent Protocol)

**Stability: Experimental**

Enables Obscura agents to discover and invoke remote agents using standard protocol bindings.

### A2A Client

```python
from obscura.integrations.a2a.client import A2AClient

client = A2AClient("https://remote-agent.example.com")

# Discover agent capabilities
card = await client.discover()  # Fetches /.well-known/agent.json

# Send task (blocking)
task = await client.send_message("Analyze this data")

# Stream task
async for event in client.stream_message("Process in real-time"):
    print(event)
```

### A2A Server

When `OBSCURA_A2A_ENABLED=true`, Obscura exposes itself as an A2A agent:

| Endpoint | Protocol | Description |
|----------|----------|-------------|
| `GET /.well-known/agent.json` | Discovery | Agent card |
| `POST /a2a/jsonrpc` | JSON-RPC 2.0 | Standard A2A RPC |
| `POST /a2a/tasks` | REST | Create task |
| `GET /a2a/tasks/{id}` | REST | Get task status |
| `GET /a2a/tasks/{id}/stream` | SSE | Stream task events |

### Transport Support

| Transport | Status | Use Case |
|-----------|--------|----------|
| JSON-RPC | Supported | Standard A2A protocol |
| REST | Supported | Simple HTTP integration |
| SSE | Supported | Real-time streaming |
| gRPC | Supported | High-performance |

### A2A as Tool Provider

Remote agents can be registered as tools for local agents:

```python
from obscura.integrations.a2a.tool_adapter import a2a_agent_as_tool

tool_spec = a2a_agent_as_tool(
    agent_url="https://remote-agent.example.com",
    name="remote-reviewer",
    description="Remote code review agent"
)
backend.register_tool(tool_spec)
```

Or via the spawn API:

```bash
curl -X POST http://localhost:8080/api/v1/agents \
  -d '{
    "name": "coordinator",
    "model": "claude",
    "a2a_remote_tools": {
      "reviewer": "https://review-agent.example.com",
      "tester": "https://test-agent.example.com"
    }
  }'
```

### Task Store

A2A tasks are persisted with two backends:

| Backend | Config | Description |
|---------|--------|-------------|
| `InMemoryTaskStore` | Default | Development, single-instance |
| `RedisTaskStore` | `OBSCURA_A2A_REDIS_URL` | Production, multi-instance |

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OBSCURA_A2A_ENABLED` | `false` | Enable A2A protocol |
| `OBSCURA_A2A_REDIS_URL` | -- | Redis URL for task store |
| `OBSCURA_A2A_AGENT_NAME` | `obscura` | Agent name in card |
| `OBSCURA_A2A_AGENT_DESCRIPTION` | -- | Agent description |
| `OBSCURA_A2A_TASK_TTL` | -- | Task expiration time |
