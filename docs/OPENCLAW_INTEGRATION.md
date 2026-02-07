# OpenClaw Integration

> Connect Obscura to OpenClaw for seamless agent orchestration.

---

## Overview

This guide shows how to connect Obscura's agent runtime to OpenClaw, enabling:

- **Spawn agents from chat** — "@obscura spawn a code reviewer"
- **Agent memory in OpenClaw** — Access shared memory from your OpenClaw agent
- **Multi-agent workflows** — Coordinate Obscura agents from OpenClaw

---

## Architecture

```
OpenClaw Agent (you)
       │
       │ HTTP/WebSocket
       ▼
┌──────────────────┐
│   Obscura API    │  localhost:8080
│   (FastAPI)      │
└────────┬─────────┘
         │
    ┌────┴────┐
    ▼         ▼
 Agents    Memory
 Runtime   Store
```

---

## Setup

### 1. Start Obscura Server

```bash
cd ~/dev/obscura
uv run python -m uvicorn sdk.server:create_app --factory --reload --port 8080
```

### 2. Configure OpenClaw

Add to your OpenClaw `TOOLS.md` or system prompt:

```markdown
## Obscura Integration

You can spawn and manage AI agents via the Obscura API at http://localhost:8080.

### Available Commands

- **Spawn Agent**: POST /api/v1/agents
- **Run Task**: POST /api/v1/agents/{id}/run
- **Check Status**: GET /api/v1/agents/{id}
- **Store Memory**: POST /api/v1/memory/{ns}/{key}
- **Semantic Search**: GET /api/v1/vector-memory/search?q={query}

### Example Usage

When the user asks you to "review this code", you should:
1. Spawn a code-review agent
2. Give it the code
3. Return the results
```

### 3. Add Obscura Client to OpenClaw

Create `obscura_client.py` in your OpenClaw workspace:

```python
"""OpenClaw client for Obscura API."""

import os
from typing import Any
import httpx

OBSCURA_BASE = os.environ.get("OBSCURA_URL", "http://localhost:8080")

class ObscuraClient:
    """Client for interacting with Obscura from OpenClaw."""
    
    def __init__(self, token: str | None = None):
        self.token = token or os.environ.get("OBSCURA_TOKEN", "local-dev-token")
        self.client = httpx.AsyncClient(
            base_url=OBSCURA_BASE,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=300.0,
        )
    
    async def spawn_agent(
        self,
        name: str,
        model: str = "claude",
        system_prompt: str = "",
        memory_namespace: str = "openclaw",
    ) -> dict[str, Any]:
        """Spawn a new agent."""
        resp = await self.client.post(
            "/api/v1/agents",
            json={
                "name": name,
                "model": model,
                "system_prompt": system_prompt,
                "memory_namespace": memory_namespace,
            }
        )
        resp.raise_for_status()
        return resp.json()
    
    async def run_agent(self, agent_id: str, prompt: str, **context) -> dict[str, Any]:
        """Run a task on an agent."""
        resp = await self.client.post(
            f"/api/v1/agents/{agent_id}/run",
            json={"prompt": prompt, "context": context},
        )
        resp.raise_for_status()
        return resp.json()
    
    async def get_agent_status(self, agent_id: str) -> dict[str, Any]:
        """Get agent status."""
        resp = await self.client.get(f"/api/v1/agents/{agent_id}")
        resp.raise_for_status()
        return resp.json()
    
    async def store_memory(
        self,
        key: str,
        value: Any,
        namespace: str = "openclaw",
    ) -> None:
        """Store a value in shared memory."""
        await self.client.post(
            f"/api/v1/memory/{namespace}/{key}",
            json={"value": value},
        )
    
    async def get_memory(self, key: str, namespace: str = "openclaw") -> Any:
        """Get a value from shared memory."""
        resp = await self.client.get(f"/api/v1/memory/{namespace}/{key}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("value")
    
    async def semantic_search(self, query: str, top_k: int = 3) -> list[dict]:
        """Search vector memories semantically."""
        resp = await self.client.get(
            "/api/v1/vector-memory/search",
            params={"q": query, "top_k": top_k},
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    
    async def close(self):
        await self.client.aclose()


# Singleton instance
_obscura_client: ObscuraClient | None = None

async def get_obscura() -> ObscuraClient:
    """Get or create Obscura client."""
    global _obscura_client
    if _obscura_client is None:
        _obscura_client = ObscuraClient()
    return _obscura_client
```

---

## Usage Examples

### Example 1: Spawn a Code Reviewer

```python
from obscura_client import get_obscura

async def review_code(code: str) -> str:
    obscura = await get_obscura()
    
    # Spawn agent
    agent = await obscura.spawn_agent(
        name="pr-reviewer",
        model="claude",
        system_prompt="You are an expert code reviewer. Focus on security, performance, and maintainability.",
    )
    agent_id = agent["agent_id"]
    
    # Run review
    result = await obscura.run_agent(
        agent_id,
        f"Review this code:\n\n```python\n{code}\n```"
    )
    
    return result.get("result", "No result")
```

### Example 2: Persistent Memory

```python
async def remember_context(context: str):
    """Store context for future sessions."""
    obscura = await get_obscura()
    
    await obscura.store_memory(
        "last_project",
        {"context": context, "timestamp": datetime.now().isoformat()},
        namespace="session"
    )

async def recall_context():
    """Retrieve previous context."""
    obscura = await get_obscura()
    return await obscura.get_memory("last_project", namespace="session")
```

### Example 3: Semantic Memory

```python
async def learn_and_recall():
    obscura = await get_obscura()
    
    # Store some knowledge
    await obscura.client.post(
        "/api/v1/vector-memory/docs/python-async",
        json={
            "text": "Python async/await uses an event loop to handle concurrency. Use asyncio.gather() to run multiple tasks.",
            "metadata": {"topic": "python", "level": "intermediate"}
        }
    )
    
    # Later, search semantically
    results = await obscura.semantic_search(
        "how do I run multiple tasks at once?",
        top_k=1
    )
    
    return results[0]["text"] if results else "No memory found"
```

### Example 4: Multi-Agent Workflow

```python
async def full_code_review(code: str, pr_description: str):
    """Run a multi-agent review workflow."""
    obscura = await get_obscura()
    
    # Spawn multiple specialists
    analyzer = await obscura.spawn_agent(
        "analyzer", "claude", "Analyze code structure and complexity"
    )
    tester = await obscura.spawn_agent(
        "tester", "claude", "Suggest comprehensive tests"
    )
    
    # Run in parallel
    analysis_task = obscura.run_agent(
        analyzer["agent_id"],
        f"Analyze this code:\n{code}"
    )
    test_task = obscura.run_agent(
        tester["agent_id"],
        f"Suggest tests for this code:\n{code}"
    )
    
    analysis, tests = await asyncio.gather(analysis_task, test_task)
    
    return {
        "analysis": analysis.get("result"),
        "tests": tests.get("result"),
    }
```

---

## System Prompt Integration

Add this to your OpenClaw `SOUL.md` or `AGENTS.md`:

```markdown
You have access to the Obscura agent platform. Use it when:

1. The user asks for specialized tasks (code review, doc writing, testing)
2. The task can be parallelized across multiple agents
3. You need to store information for later sessions
4. You want to search previous conversations semantically

### Available Commands

**Spawn an agent:**
```
POST /api/v1/agents
{"name": "reviewer", "model": "claude", "system_prompt": "..."}
```

**Run a task:**
```
POST /api/v1/agents/{id}/run
{"prompt": "Review this code: ...", "context": {"file": "main.py"}}
```

**Store memory:**
```
POST /api/v1/memory/{namespace}/{key}
{"value": {"data": "..."}}
```

**Semantic search:**
```
GET /api/v1/vector-memory/search?q={query}
```

### Workflow

When asked to perform a complex task:
1. Consider if an agent would do it better
2. Spawn appropriate agent(s)
3. Give them the context
4. Return their results to the user
5. Store important findings in memory for later
```

---

## Testing the Integration

```bash
# 1. Start Obscura
cd ~/dev/obscura
uv run python -m uvicorn sdk.server:create_app --factory --port 8080

# 2. In OpenClaw, test the client
python -c "
import asyncio
from obscura_client import get_obscura

async def test():
    o = await get_obscura()
    
    # Test memory
    await o.store_memory('test', {'hello': 'world'})
    val = await o.get_memory('test')
    print(f'Memory: {val}')
    
    # Test agent spawn
    agent = await o.spawn_agent('test-agent', 'claude', 'You are a test')
    print(f'Agent: {agent}')

asyncio.run(test())
"
```

---

## Security Considerations

1. **Token Management** — Use environment variables, never hardcode tokens
2. **Auth Scope** — OpenClaw should use a service account with limited permissions
3. **Network** — In production, use TLS and internal network only
4. **Rate Limiting** — Add rate limits to prevent abuse

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Connection refused | Make sure Obscura server is running on port 8080 |
| 401 Unauthorized | Check OBSCURA_TOKEN environment variable |
| Agent spawn fails | Verify model name ("claude" or "copilot") |
| Memory not persisting | Check SQLite permissions in ~/.obscura/memory/ |

---

## Future Enhancements

- **WebSocket streaming** — Real-time agent output
- **Agent templates** — Pre-configured agents for common tasks
- **OpenClaw plugin** — Native `/spawn` and `/memory` commands
- **Visual agent monitor** — See all running agents in OpenClaw UI
