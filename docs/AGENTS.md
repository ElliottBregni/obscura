# Agent Runtime — Building Agents on Obscura

> Spawn, manage, and coordinate AI agents with shared memory.

---

## Overview

The Agent Runtime provides **lifecycle management for AI agents** on top of Obscura's memory system:

- **Spawn agents** — Create isolated agent instances with their own config
- **Manage state** — Track agent status (pending → running → completed/failed)
- **Shared memory** — Agents read/write to the same memory store (scoped by user)
- **Message passing** — Agents can communicate with each other
- **Persistence** — Agent state survives restarts

Think of it as a **"process manager for AI agents."**

---

## Quick Start

### Python SDK

```python
from sdk.agent.agents import AgentRuntime
from sdk.auth.models import AuthenticatedUser

# Create runtime
user = AuthenticatedUser(...)  # From JWT
runtime = AgentRuntime(user)
await runtime.start()

# Spawn an agent
agent = runtime.spawn(
    name="code-reviewer",
    model="claude",
    system_prompt="You are an expert code reviewer...",
    memory_namespace="project:obscura"
)

# Start and run
await agent.start()
result = await agent.run("Review this PR: ...")

# Cleanup
await agent.stop()
await runtime.stop()
```

### Convenience Method

```python
# Spawn, start, run, and get result in one call
agent, result = await runtime.spawn_and_run(
    name="doc-writer",
    prompt="Write docs for the auth module",
    model="claude",
    system_prompt="You write technical documentation...",
    memory_namespace="docs"
)
```

---

## Agent Lifecycle

```
┌─────────┐    start()    ┌─────────┐    run()     ┌───────────┐
│ PENDING │ ─────────────→│ WAITING │ ────────────→│  RUNNING  │
└─────────┘               └─────────┘              └─────┬─────┘
                                                         │
                            ┌────────────┐              │
                            │  COMPLETED │ ←─────────────┤ success
                            └────────────┘              │
                            ┌────────────┐              │ failure
                            │   FAILED   │ ←─────────────┘
                            └────────────┘
                            ┌────────────┐
         stop() ───────────→│   STOPPED  │
                            └────────────┘
```

---

## Agent Memory

Agents automatically use the shared memory system:

```python
# Agent stores task context
agent.memory.set(
    "task_0",
    {"prompt": "Review PR", "started_at": "..."},
    namespace="project:obscura:tasks"
)

# Agent loads relevant memory before running
memory = agent._load_relevant_memory(prompt)
# Returns last 5 tasks + search results
```

**Automatic prompt enrichment:**
```
## Relevant Context from Memory:
- project:obscura:tasks/task_0: {"prompt": "Review PR", ...}
- project:obscura:tasks/task_1: {"prompt": "Fix bug", ...}

## Task Context:
- extra: context values

## Task:
<user prompt here>
```

---

## Agent Communication

Agents can send messages to each other:

```python
# Agent A sends to Agent B
await agent_a.send_message(agent_b.id, "I finished the review")

# Agent B receives
async for message in agent_b.receive_messages():
    print(f"From {message.source}: {message.content}")

# Broadcast to all agents
await agent_a.send_message("broadcast", "Shutting down soon")
```

---

## HTTP API

### Spawn Agent
```bash
curl -X POST http://localhost:8080/api/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "code-reviewer",
    "model": "claude",
    "system_prompt": "You are an expert code reviewer...",
    "memory_namespace": "project:obscura",
    "max_iterations": 10
  }'
```
Response:
```json
{
  "agent_id": "agent-a1b2c3d4",
  "name": "code-reviewer",
  "status": "PENDING",
  "created_at": "2024-01-15T10:30:00"
}
```

### Run Task
```bash
curl -X POST http://localhost:8080/api/v1/agents/agent-a1b2c3d4/run \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Review this PR: https://github.com/...",
    "context": {"pr_number": 123}
  }'
```

### Get Agent Status
```bash
curl http://localhost:8080/api/v1/agents/agent-a1b2c3d4 \
  -H "Authorization: Bearer $TOKEN"
```
Response:
```json
{
  "agent_id": "agent-a1b2c3d4",
  "name": "code-reviewer",
  "status": "COMPLETED",
  "created_at": "2024-01-15T10:30:00",
  "updated_at": "2024-01-15T10:31:30",
  "iteration_count": 1,
  "error_message": null
}
```

### List Agents
```bash
curl "http://localhost:8080/api/v1/agents?status=RUNNING" \
  -H "Authorization: Bearer $TOKEN"
```

### Stop Agent
```bash
curl -X DELETE http://localhost:8080/api/v1/agents/agent-a1b2c3d4 \
  -H "Authorization: Bearer $TOKEN"
```

---

## Configuration

### AgentConfig Options

```python
@dataclass
class AgentConfig:
    name: str                    # Human-readable name
    model: str                   # "copilot" or "claude"
    system_prompt: str = ""      # System instructions
    memory_namespace: str = "default"  # Memory isolation
    max_iterations: int = 10     # Safety limit
    timeout_seconds: float = 300 # Task timeout
    tools: list[str] = []        # Tool names to enable
    parent_agent_id: str | None = None  # For agent hierarchies
```

---

## Advanced Patterns

### Multi-Agent Workflow

```python
# Spawn multiple agents
reviewer = runtime.spawn("reviewer", model="claude")
tester = runtime.spawn("tester", model="claude")
doc_writer = runtime.spawn("doc-writer", model="claude")

# Run them in parallel
await asyncio.gather(
    reviewer.run("Review the code"),
    tester.run("Write tests"),
    doc_writer.run("Write docs")
)

# Wait for completion
states = await runtime.wait_for_agents(
    [reviewer.id, tester.id, doc_writer.id],
    timeout=300
)
```

### Agent Hierarchies

```python
# Parent agent spawns child
child = runtime.spawn(
    name="researcher",
    model="claude",
    parent_agent_id=parent_agent.id
)

# Child inherits parent's memory namespace
# Parent can monitor child's progress
```

### Streaming Responses

```python
async for chunk in agent.stream("Write a long document"):
    print(chunk, end="")
```

---

## State Persistence

Agent state is automatically saved to memory:

```python
# Saved after each status change
{
    "agent_id": "agent-xxx",
    "name": "code-reviewer",
    "status": "COMPLETED",
    "created_at": "2024-01-15T10:30:00",
    "updated_at": "2024-01-15T10:31:30",
    "iteration_count": 5,
    "error_message": null
}
```

**Survives restarts:**
```python
# Server restarts...
runtime = AgentRuntime(user)
state = runtime.get_agent_status("agent-xxx")  # Still works!
```

---

## Architecture

```
┌─────────────────────────────────────────┐
│         AgentRuntime                    │
│  - Spawns agents                        │
│  - Routes messages                      │
│  - Manages lifecycle                    │
└──────┬────────────────┬─────────────────┘
       │                │
       ▼                ▼
┌──────────┐      ┌──────────┐
│  Agent 1 │◄────►│  Agent 2 │
│ (claude) │      │ (copilot)│
└────┬─────┘      └────┬─────┘
     │                 │
     └────────┬────────┘
              ▼
      ┌───────────────┐
      │  MemoryStore  │  (shared per user)
      │   for_user()  │
      └───────┬───────┘
              ▼
         SQLite DB
     (isolated per user)
```

---

## Testing

```bash
# Run agent tests
uv run pytest tests/test_agents.py -v

# Run specific test
uv run pytest tests/test_agents.py::TestAgentRuntime -v
```

---

## Semantic Memory Integration

Agents can use vector memory for semantic recall via the `SemanticMemoryMixin`:

```python
from sdk.vector_memory import SemanticMemoryMixin

# After mixin integration, agents get remember() and recall()
agent.remember("The auth module uses JWT with RS256")
results = agent.recall("how is authentication done?")
```

See [docs/VECTOR_MEMORY.md](VECTOR_MEMORY.md) for full vector memory documentation.

---

## State Persistence Notes

Agent **state data** (status, iteration count, errors) is persisted to memory and can be read back after restarts via `runtime.get_agent_status(agent_id)`. However, the **agent instance itself** (client connection, message queue) is not reconstructable from state alone — a new agent must be spawned for further tasks.

---

## Future Enhancements

- **Agent discovery** — Find agents by capability/tags
- **Agent marketplace** — Share agent configs
- **Agent checkpoints** — Save/restore full agent state mid-task
- **Distributed agents** — Run agents on multiple machines
