**Multi-agent context management system** for code repositories. Keep skills, instructions, and architectural knowledge in one place, synced across multiple AI agents (GitHub Copilot, Claude, etc.) and repositories.

Now with **Agent Runtime** — spawn, manage, and coordinate AI agents with shared memory.

---

## 🎯 What This Is

A **single source of truth** for LLM context that:
- ✅ Works with **any AI agent** (Copilot, Claude, Cursor, or custom)
- ✅ Syncs to **any number of code repositories**
- ✅ Uses **symlinks** for zero-copy, instant updates
- ✅ Supports **universal** and **agent-specific** overrides
- ✅ Keeps **vault separate** from code repos (no PRs for context iteration)
- ✅ Works with **Obsidian** or any Markdown editor

### 🆕 Agent Runtime (NEW)
- ✅ **Spawn agents** — Create isolated AI agents with their own config
- ✅ **Shared memory** — Multi-tenant storage scoped by auth token
- ✅ **Agent coordination** — Message passing between agents
- ✅ **State persistence** — Agents survive server restarts
- ✅ **HTTP API** — RESTful control of agent lifecycle

---

## 🚀 Quick Start

### 1. **Add a Repository**
```bash
cd ~/obscura
# Add repo path to repos/INDEX.md
echo "~/git/YourRepo" >> repos/INDEX.md
```

### 2. **Sync**
```bash
# Sync in symlink mode (instant, zero-copy)
python3 sync.py --repo ~/git/YourRepo --agent copilot --mode symlink
```

### 3. **Edit & Iterate**
- Edit files in `~/obscura/skills/` or `instructions/`
- Changes instantly appear in all linked repos via symlinks
- No git commits needed until you're ready

---

## 🤖 Agent Runtime (New)

Spawn and manage AI agents with shared memory:

```python
from sdk.agents import AgentRuntime

# Create runtime
runtime = AgentRuntime(user)
await runtime.start()

# Spawn an agent
agent = runtime.spawn(
    name="code-reviewer",
    model="claude",
    system_prompt="You are an expert code reviewer...",
    memory_namespace="project:obscura"
)

# Run it
await agent.start()
result = await agent.run("Review this PR: ...")

# Agents share memory
agent.memory.set("last_review", {"pr": 123, "status": "approved"})
```

Or use the HTTP API:
```bash
# Spawn agent
curl -X POST http://localhost:8080/api/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "reviewer", "model": "claude"}'

# Run task
curl -X POST http://localhost:8080/api/v1/agents/agent-xxx/run \
  -d '{"prompt": "Review this code"}'
```

See [docs/AGENTS.md](docs/AGENTS.md) for full documentation.

---

## 🧠 Vector Memory (Semantic Search)

Store memories with embeddings and retrieve by meaning:

```python
from sdk.vector_memory import VectorMemoryStore

store = VectorMemoryStore.for_user(user)

# Store with automatic embedding
store.set("async_guide", "Python async/await handles concurrency with an event loop")
store.set("threading_guide", "Python threading is best for I/O-bound tasks")

# Semantic search — finds related even without keyword match
results = store.search_similar("how to run things in parallel?", top_k=3)
for r in results:
    print(f"{r.key}: score={r.score:.2f}")
```

See [docs/VECTOR_MEMORY.md](docs/VECTOR_MEMORY.md) for full documentation.

---

## 💾 Shared Memory

Auth-scoped key-value storage for agents:

```python
from sdk.memory import MemoryStore

store = MemoryStore.for_user(user)
store.set("context", {"repo": "obscura"}, namespace="session")
value = store.get("context", namespace="session")
```

See [docs/MEMORY.md](docs/MEMORY.md) for full documentation.

---

## ⌨️ CLI Quick Start

```bash
# Start server
obscura serve --port 8080

# Agent management
obscura agent spawn --name reviewer --model claude
obscura agent run <id> --prompt "Review this code"
obscura agent list --status RUNNING
obscura agent stop <id>

# Memory operations
obscura memory set context '{"repo": "obscura"}' --namespace session
obscura memory get context --namespace session
obscura memory search "database"

# Vector memory (semantic)
obscura vector remember "Python async/await handles concurrency"
obscura vector recall "how to handle parallel tasks" --top-k 3
```

---

## 📂 Structure

### **Core Directories**

| Directory | Purpose | Sync Target |
|-----------|---------|-------------|
| **`skills/`** | Skill files for LLMs | → `repo/.github/skills/` (copilot)<br>→ `repo/.claude/skills/` (claude) |
| **`instructions/`** | Instruction sets for LLMs | → `repo/.github/instructions/` (copilot)<br>→ `repo/.claude/instructions/` (claude) |
| **`agents/`** | Agent registry (`INDEX.md`) | Reference only |
| **`repos/`** | Repository index (`INDEX.md`) | Links to code repos |
| **`docs/`** | Vault documentation | Internal only |
| **`git-hooks/`** | Shared git hooks | → `repo/.git/hooks/` |

### **Universal vs Agent-Specific Files**

The vault supports **three routing patterns** to manage shared and agent-specific content:

#### 1️⃣ **Universal Files** (Shared Across All Agents)
```
skills/
├── setup.md              # Used by all agents
├── git-workflow.md       # Shared everywhere
└── testing.md            # Universal testing guide
```

#### 2️⃣ **Directory-Level Agent Overrides**
```
skills/
├── setup.md              # Universal fallback
├── skills.copilot/       # Copilot-specific directory
│   ├── api-design.md     # Copilot only
│   └── python.md         # Overrides universal python.md
└── skills.claude/        # Claude-specific directory
    └── database.md       # Claude only
```

#### 3️⃣ **Nested File Overrides** (Recommended)
```
instructions/
├── x.md                  # Universal version
├── x.copilot.md          # Copilot override (same directory)
├── x.claude.md           # Claude override (same directory)
├── y.copilot.md          # Copilot-only (no universal)
└── z.md                  # Universal only (no overrides)
```

**Priority Rules:**
- Directory-level (`skills.copilot/`) > Nested file (`skills/x.copilot.md`) > Universal (`skills/x.md`)
- Agent-specific always wins over universal
- Missing agent-specific file? Falls back to universal

See [docs/AGENT-ROUTING.md](docs/AGENT-ROUTING.md) for full details.

---

## 🔄 Sync Modes

### **Symlink Mode** (Recommended)
Zero-copy, instant sync with multi-agent support. Changes in vault appear immediately in repos.

```bash
# Link for a specific agent
python3 sync.py --repo ~/git/MyRepo --agent copilot --mode symlink

# Link for all registered agents
python3 sync.py --repo ~/git/MyRepo --mode symlink
```

**Features:**
- Multi-agent routing (copilot → `.github/`, claude → `.claude/`)
- Multiple agents per repo (both `.github/` and `.claude/` can coexist)
- Recursive directory-matching sync (vault tree → repo tree)
- Broken symlink detection and auto-repair
- Agent validation (ensures agent is registered before linking)

**Pros:** Instant, no duplication, edit anywhere, multi-agent support
**Cons:** Git operations can break symlinks (auto-repair available via git hooks)

### **Copy Mode**
One-way file copy from vault to repo.

```bash
python3 sync.py --repo ~/git/MyRepo --agent copilot --mode copy
```

**Pros:** Git-safe, no symlink issues
**Cons:** Manual sync required for changes

### **Watch Mode**
Continuous background sync using fswatch. Auto-syncs when vault files change.

```bash
python3 sync.py --watch
```

**Pros:** Hands-free, re-syncs on every vault edit
**Cons:** Requires fswatch (`brew install fswatch`)

---

## 📚 Documentation

### **Getting Started**
- [📦 Installation Guide](docs/INSTALL.md) - Full setup instructions
- [⚡ Quick Start](docs/QUICKSTART.md) - Get running in 5 minutes
- [🔗 GitHub Integration](docs/GITHUB-INTEGRATION.md) - Sync strategies
- [📝 No Obsidian?](docs/NO-OBSIDIAN.md) - Use any Markdown editor

### **New Features**
- [🤖 Agent Runtime](docs/AGENTS.md) - Spawn, manage, and coordinate AI agents
- [💾 Shared Memory](docs/MEMORY.md) - Multi-tenant auth-scoped key-value storage
- [🧠 Vector Memory](docs/VECTOR_MEMORY.md) - Semantic search with embeddings
- [🔗 OpenClaw Integration](docs/OPENCLAW_INTEGRATION.md) - Agent spawning from OpenClaw

### **Advanced Topics**
- [🎭 Agent Routing](docs/AGENT-ROUTING.md) - Multi-agent patterns & priority rules
- [🤖 Auto-Sync](docs/AUTO-SYNC.md) - Background sync with launchd
- [🔧 MCP Config](docs/MCP-README.md) - Model Context Protocol setup
- [📊 Migration Notes](docs/MIGRATION-NOTES.md) - Upgrading from old structures

---

## 🛠️ Common Workflows

### **Add a New Skill**
```bash
# Create universal skill (all agents)
echo "# Python Best Practices" > skills/python.md

# Or create agent-specific skill
echo "# Copilot Python Tips" > skills/python.copilot.md
```

### **Override an Instruction for One Agent**
```bash
# Universal instruction exists at instructions/setup.md
# Create Copilot-specific override:
cp instructions/setup.md instructions/setup.copilot.md
# Edit setup.copilot.md - only Copilot sees changes
```

### **Sync Multiple Repos**
```bash
# List all repos in repos/INDEX.md
cat repos/INDEX.md
# Output:
# ~/git/FV-Platform-Main
# ~/git/OtherProject

# Sync all repos for Copilot
python3 sync.py --agent copilot --mode symlink

# Sync all repos for all registered agents
python3 sync.py --mode symlink

# Sync specific repo for specific agent
python3 sync.py --repo ~/git/MyRepo --agent copilot --mode symlink
```

### **Test Before Committing**
```bash
# 1. Link repo in symlink mode
python3 sync.py --repo ~/git/MyRepo --agent copilot --mode symlink

# 2. Edit skills/instructions in vault
# 3. Test in your IDE (changes are live via symlinks)
# 4. When ready, convert symlinks to real files:
python3 sync.py --merge --repo ~/git/MyRepo

# 5. Commit in repo
cd ~/git/MyRepo && git add .github/ && git commit
```

---

## 🎭 Multi-Agent Support

Register agents in `agents/INDEX.md`:
```markdown
# Active Agents

- copilot    → .github/
- claude     → .claude/
- cursor     → .cursor/
```

Each agent gets its own target path in repositories. Files route automatically based on agent suffix or directory name.

**Example:**
- `skills.copilot/api.md` → `repo/.github/skills/api.md`
- `skills.claude/api.md` → `repo/.claude/skills/api.md`
- `skills/api.md` → Both agents see it (universal)

---

## 🔍 Troubleshooting

### **Symlinks Not Working?**
```bash
# Check if symlinks exist
ls -la ~/git/MyRepo/.github/

# Re-link if broken
python3 sync.py --repo ~/git/MyRepo --agent copilot --mode symlink
```

### **Agent Not Seeing Files?**
1. Check agent is registered in `agents/INDEX.md`
2. Verify symlink exists: `ls -la ~/git/MyRepo/.github` (copilot) or `~/.claude` (claude)
3. Check symlink target: `readlink ~/git/MyRepo/.github`
4. Verify file naming: `filename.copilot.md` or `directory.copilot/`
5. Check priority: directory-level > nested file > universal
6. See [Agent Routing Docs](docs/AGENT-ROUTING.md#troubleshooting)

### **Wrong Agent Path?**
Each agent has a specific target path:
- `copilot` → `.github/`
- `claude` → `.claude/`
- `cursor` → `.cursor/`
- Custom agents → `.agent-name/`

Check the symlink is pointing to the right directory for your agent.

### **Git Rebase Broke Symlinks?**
```bash
# Auto-repair with post-merge hook
cp git-hooks/post-merge ~/git/MyRepo/.git/hooks/
chmod +x ~/git/MyRepo/.git/hooks/post-merge

# Or manually re-link
python3 sync.py --repo ~/git/MyRepo --agent copilot --mode symlink
```

---

## 🧪 Testing

After syncing, verify what each agent sees:

```bash
# Test Copilot
ls -la ~/git/MyRepo/.github/skills/
cat ~/git/MyRepo/.github/skills/python.md

# Test Claude
ls -la ~/git/MyRepo/.claude/skills/
cat ~/git/MyRepo/.claude/skills/python.md
```

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      obscura Vault                        │
│  (Single Source of Truth)                                    │
│                                                               │
│  skills/                instructions/           agents/      │
│  ├── universal.md       ├── x.md                ├── INDEX.md │
│  ├── skill.copilot.md  ├── x.copilot.md        └── copilot  │
│  └── skill.claude.md   └── x.claude.md             claude    │
└───────────────┬─────────────────────────────────────────────┘
                │
                │ sync.py (symlink/copy/watch)
                │
        ┌───────┴────────┬────────────────┐
        ▼                ▼                ▼
  ┌─────────┐      ┌─────────┐      ┌─────────┐
  │ Repo A  │      │ Repo B  │      │ Repo C  │
  │         │      │         │      │         │
  │.github/ │      │.github/ │      │.claude/ │
  │.claude/ │      │.cursor/ │      │.github/ │
  └─────────┘      └─────────┘      └─────────┘
```

**Key Principles:**
- Vault is the **source of truth**
- Repos are **targets** (symlinks or copies)
- Agent routing is **automatic** (based on file naming)
- Priority is **deterministic** (directory > nested > universal)

---

## 📖 Usage Examples

### Memory Store — Python SDK

```python
from sdk.memory import MemoryStore
from sdk.auth.models import AuthenticatedUser
from datetime import timedelta

user = AuthenticatedUser(user_id="u-1", email="dev@example.com",
                         roles=("agent:claude",), org_id="org-1",
                         token_type="user", raw_token="...")
store = MemoryStore.for_user(user)

# Basic CRUD
store.set("context", {"repo": "obscura", "branch": "main"}, namespace="session")
value = store.get("context", namespace="session")
# → {"repo": "obscura", "branch": "main"}

# TTL — auto-expire after 5 minutes
store.set("cache_key", {"data": "temporary"}, namespace="cache", ttl=timedelta(minutes=5))

# Search across keys and values
results = store.search("obscura")
# → [(MemoryKey(session:context), {"repo": "obscura", ...})]

# Namespace operations
keys = store.list_keys(namespace="session")
store.clear_namespace("cache")

# Stats
stats = store.get_stats()
# → {"total_keys": 5, "expired_keys": 1, "namespaces": {"session": 3, "cache": 2}}
```

### Agent Runtime — Python SDK

```python
from sdk.agents import AgentRuntime
import asyncio

runtime = AgentRuntime(user)
await runtime.start()

# Single agent
agent = runtime.spawn(
    "code-reviewer",
    model="claude",
    system_prompt="You are an expert code reviewer. Focus on security and performance.",
    memory_namespace="project:myapp",
)
await agent.start()
result = await agent.run("Review this function:\ndef process(data): return eval(data)")
print(result)  # Security warning about eval()

# Multi-agent parallel workflow
reviewer = runtime.spawn("reviewer", model="claude")
tester = runtime.spawn("tester", model="claude")
doc_writer = runtime.spawn("doc-writer", model="claude")

await asyncio.gather(reviewer.start(), tester.start(), doc_writer.start())
results = await asyncio.gather(
    reviewer.run("Review the auth module"),
    tester.run("Write tests for the auth module"),
    doc_writer.run("Document the auth module API"),
)

# Agent-to-agent communication
await reviewer.send_message(tester.id, "Review complete — found 2 issues in token validation")
async for msg in tester.receive_messages():
    print(f"From {msg.source}: {msg.content}")

# Wait for all agents
states = await runtime.wait_for_agents(
    [reviewer.id, tester.id, doc_writer.id],
    timeout=300,
)

# Cleanup
await runtime.stop()
```

### Vector Memory — Python SDK

```python
from sdk.vector_memory import VectorMemoryStore

store = VectorMemoryStore.for_user(user)

# Store knowledge with automatic embedding
store.set("async_guide", "Python async/await handles concurrency using an event loop. "
          "It's ideal for I/O-bound operations like HTTP requests and database queries.")
store.set("threading_guide", "Python threading runs multiple threads in the same process. "
          "Best for I/O-bound tasks. The GIL prevents true parallel CPU execution.")
store.set("multiprocessing_guide", "Python multiprocessing spawns separate processes. "
          "Bypasses the GIL for true CPU parallelism.")

# Semantic search — finds related memories even without keyword match
results = store.search_similar("how do I run multiple things at once?", top_k=3)
for r in results:
    print(f"  [{r.score:.2f}] {r.key.key}: {r.text[:80]}...")

# Store with metadata
store.set("auth_pattern", "JWT tokens with RS256 signing and JWKS rotation",
          metadata={"module": "auth", "importance": "high"}, namespace="architecture")

# Search within a namespace
results = store.search_similar("security", namespace="architecture", top_k=5)
```

### HTTP API — curl Examples

```bash
# === Agent Lifecycle ===

# Spawn an agent
curl -X POST http://localhost:8080/api/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "reviewer", "model": "claude", "system_prompt": "You review code."}'
# → {"agent_id": "agent-a1b2c3d4", "name": "reviewer", "status": "WAITING"}

# Run a task
curl -X POST http://localhost:8080/api/v1/agents/agent-a1b2c3d4/run \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Review: def login(pw): return md5(pw)", "context": {"file": "auth.py"}}'
# → {"agent_id": "agent-a1b2c3d4", "status": "COMPLETED", "result": "..."}

# Stream a task (SSE)
curl -N -X POST http://localhost:8080/api/v1/agents/agent-a1b2c3d4/stream \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write a detailed security report"}'

# Check status
curl http://localhost:8080/api/v1/agents/agent-a1b2c3d4 \
  -H "Authorization: Bearer $TOKEN"

# List running agents
curl "http://localhost:8080/api/v1/agents?status=RUNNING" \
  -H "Authorization: Bearer $TOKEN"

# Stop agent
curl -X DELETE http://localhost:8080/api/v1/agents/agent-a1b2c3d4 \
  -H "Authorization: Bearer $TOKEN"

# === Memory ===

# Store
curl -X POST http://localhost:8080/api/v1/memory/session/context \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"value": {"repo": "obscura", "task": "code review"}}'

# Store with TTL (300 seconds)
curl -X POST "http://localhost:8080/api/v1/memory/cache/temp?ttl=300" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"value": "expires in 5 minutes"}'

# Get
curl http://localhost:8080/api/v1/memory/session/context \
  -H "Authorization: Bearer $TOKEN"

# Search
curl "http://localhost:8080/api/v1/memory/search?q=obscura" \
  -H "Authorization: Bearer $TOKEN"

# Stats
curl http://localhost:8080/api/v1/memory/stats \
  -H "Authorization: Bearer $TOKEN"

# === Vector Memory ===

# Store with embedding
curl -X POST http://localhost:8080/api/v1/vector-memory/docs/python-async \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "Async/await for concurrency", "metadata": {"topic": "python"}}'

# Semantic search
curl "http://localhost:8080/api/v1/vector-memory/search?q=parallel+execution&top_k=3" \
  -H "Authorization: Bearer $TOKEN"
```

### OpenClaw Integration

```python
from obscura_client import get_obscura

obscura = await get_obscura()

# Spawn + run agent
agent = await obscura.spawn_agent("reviewer", "claude",
    system_prompt="Expert code reviewer")
result = await obscura.run_agent(agent["agent_id"], "Review this PR")

# Memory from OpenClaw
await obscura.memory_set("context", {"task": "review"}, namespace="session")
context = await obscura.memory_get("context", namespace="session")

# Semantic memory
await obscura.remember("The project uses FastAPI with SQLite")
results = await obscura.recall("what framework does this use?")
```

---

## 📜 License

MIT License - See repository for details.

---

## 🤝 Contributing

See [docs/MIGRATION-NOTES.md](docs/MIGRATION-NOTES.md) for development patterns and best practices.

---

## 📞 Support

- **Documentation:** [docs/](docs/) folder
- **Agent Routing:** [AGENT-ROUTING.md](docs/AGENT-ROUTING.md)
- **Auto-Sync:** [AUTO-SYNC.md](docs/AUTO-SYNC.md)
- **MCP Setup:** [MCP-README.md](docs/MCP-README.md)

---

**Built for developers who iterate fast on LLM context without spamming PRs.** 🚀
