# Agent Runtime

Spawn, manage, and coordinate AI agents with shared memory and multi-provider support.

## Concepts

- **AgentRuntime** -- Lifecycle manager. Spawns agents, routes messages, tracks state.
- **BaseAgent** -- APER loop (Analyze-Plan-Execute-Respond) with 8 hook points.
- **AgentLoop** -- Low-level tool-calling loop. Streams from backend, executes tools, injects results.
- **MemoryStore** -- Shared per-user storage. Agents read/write context, task state, and results.

## Quick Start

### Python SDK

```python
from obscura.agent.agents import AgentRuntime
from obscura.auth.models import AuthenticatedUser

user = AuthenticatedUser(...)  # From JWT
runtime = AgentRuntime(user)
await runtime.start()

# Spawn and run
agent = runtime.spawn(
    name="code-reviewer",
    model="claude",
    system_prompt="You are an expert code reviewer.",
    memory_namespace="project:obscura"
)
await agent.start()
result = await agent.run("Review this module for security issues")

# Cleanup
await agent.stop()
await runtime.stop()
```

### One-liner

```python
agent, result = await runtime.spawn_and_run(
    name="doc-writer",
    prompt="Write docs for the auth module",
    model="claude",
    system_prompt="You write technical documentation.",
    memory_namespace="docs"
)
```

### HTTP API

```bash
# Spawn
curl -X POST http://localhost:8080/api/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "reviewer", "model": "claude", "system_prompt": "Expert code reviewer."}'

# Run task
curl -X POST http://localhost:8080/api/v1/agents/agent-abc/run \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Review this PR", "context": {"pr_number": 123}}'

# Stream (SSE)
curl http://localhost:8080/api/v1/agents/agent-abc/stream \
  -H "Authorization: Bearer $TOKEN"

# Status
curl http://localhost:8080/api/v1/agents/agent-abc \
  -H "Authorization: Bearer $TOKEN"

# List (filter by status)
curl "http://localhost:8080/api/v1/agents?status=RUNNING" \
  -H "Authorization: Bearer $TOKEN"

# Stop
curl -X DELETE http://localhost:8080/api/v1/agents/agent-abc \
  -H "Authorization: Bearer $TOKEN"
```

### CLI

```bash
obscura agent spawn --name reviewer --model claude
obscura agent list
obscura agent run <agent-id> --prompt "Review this code"
obscura agent status <agent-id>
```

## Agent Lifecycle

```
PENDING --start()--> WAITING --run()--> RUNNING --success--> COMPLETED
                                            \--failure--> FAILED
                 stop() at any point --> STOPPED
```

State is persisted to memory (`agent:runtime` namespace) and survives server restarts.

## APER Loop (BaseAgent)

Subclass `BaseAgent` and override the four phase methods:

```python
from obscura.agent.agent import BaseAgent
from obscura.core.types import AgentContext

class IncidentTriager(BaseAgent):
    async def analyze(self, ctx: AgentContext) -> None:
        """Classify the incident."""
        ctx.analysis = await self._client.send(
            f"Classify this incident: {ctx.input_data}"
        )

    async def plan(self, ctx: AgentContext) -> None:
        """Determine investigation steps."""
        ctx.plan = ["check auth logs", "review recent deploys", "inspect config"]

    async def execute(self, ctx: AgentContext) -> None:
        """Run each investigation step."""
        for step in ctx.plan:
            result = await self._client.send(f"Execute: {step}")
            ctx.results.append(result)

    async def respond(self, ctx: AgentContext) -> None:
        """Compose final report."""
        ctx.response = await self._client.send(
            f"Summarize findings: {ctx.results}"
        )
```

### Hooks

Register callbacks at any phase boundary:

```python
from obscura.core.types import HookPoint

agent.on(HookPoint.PRE_EXECUTE, my_validation_callback)
agent.on(HookPoint.POST_RESPOND, my_audit_callback)
```

8 hook points: `PRE_ANALYZE`, `POST_ANALYZE`, `PRE_PLAN`, `POST_PLAN`, `PRE_EXECUTE`, `POST_EXECUTE`, `PRE_RESPOND`, `POST_RESPOND`.

## Multi-Agent Workflows

Agents share memory within a user scope, enabling coordination:

```python
# Spawn specialized agents
triage = runtime.spawn("triage", model="claude")
auth_audit = runtime.spawn("auth-audit", model="claude")
repo_inspector = runtime.spawn("repo-inspector", model="copilot")

# Run in parallel
import asyncio
await asyncio.gather(
    triage.run("Classify ticket: 502 errors in login"),
    auth_audit.run("Check auth context for JWKS issues"),
    repo_inspector.run("Inspect code ownership for auth module")
)

# Results are in shared memory -- each agent writes to default:tasks
```

### Agent Communication

```python
# Direct message
await agent_a.send_message(agent_b.id, "Review complete")

# Broadcast
await agent_a.send_message("broadcast", "Shutting down")

# Receive
async for msg in agent_b.receive_messages():
    print(f"From {msg.source}: {msg.content}")
```

## MCP Integration

Agents can use MCP servers for tool access:

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
      "server_names": ["filesystem"]
    }
  }'
```

## System Tools

Agents can execute shell commands and Python when granted privileged access:

```bash
curl -X POST http://localhost:8080/api/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "system-inspector",
    "model": "claude",
    "system_tools": {"enabled": true}
  }'
```

System tools are policy-controlled. See [TOOLS.md](TOOLS.md).

## Tool Approval Workflow

Sensitive tool calls can require human approval:

```python
# Agent requests tool execution
# -> Approval request created
# -> User approves/denies via API
# -> Agent proceeds or receives denial
```

## Configuration

```python
@dataclass
class AgentConfig:
    name: str                         # Human-readable name
    model: str                        # "copilot", "claude", "openai", etc.
    system_prompt: str = ""           # System instructions
    memory_namespace: str = "default" # Memory isolation
    max_iterations: int = 10          # Safety limit for tool loops
    timeout_seconds: float = 300      # Task timeout
    tools: list[str] = []             # Tool names to enable
    parent_agent_id: str | None = None  # For agent hierarchies
```

## Supported Models

| Model | Backend | Notes |
|-------|---------|-------|
| `copilot` | GitHub Copilot | Event-based streaming, SDK hooks |
| `claude` | Anthropic Claude | Session fork, MCP in-process |
| `openai` | OpenAI | Responses API + Chat Completions |
| `localllm` | Local server | OpenAI-compatible (localhost:1234) |
| `moonshot` | Moonshot/Kimi | Extends OpenAI backend |
