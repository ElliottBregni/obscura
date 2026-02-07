# Changelog — Obscura SDK

All notable changes to the Obscura SDK.

## [Unreleased]

### Added

#### OpenClaw Client
- **`openclaw_client.py`** — Complete client for OpenClaw integration
  - Async/await API
  - Singleton pattern for reuse
  - Convenience functions: `spawn()`, `run()`, `remember()`, `recall()`, `quick()`
  - Multi-agent workflows
  - Automatic cleanup

#### WebSocket Streaming
- **Real-time agent I/O** — WebSocket endpoints for live communication
  - `ws://localhost:8080/ws/agents/{agent_id}` — Stream agent output
  - `ws://localhost:8080/ws/monitor` — Live status updates
  - JSON protocol for commands and responses
  - Automatic reconnection support

#### Web UI — Agent Monitor
- **`web-ui/index.html`** — Beautiful dark-mode dashboard
  - Real-time agent status (via WebSocket)
  - Visual indicators: PENDING, RUNNING, COMPLETED, FAILED, STOPPED
  - Spawn agents from browser
  - Stop agents with confirmation
  - Live system logs
  - Statistics cards
  - Responsive design

#### CLI Tool
- **`obscura_cli.py`** — Full-featured command-line interface
  - **Agent commands:** spawn, list, run, stop, status, quick
  - **Memory commands:** set, get, delete, list, search
  - **Vector commands:** remember, recall (semantic search)
  - **Server command:** serve (start API server)
  - Rich terminal output with colors and tables
  - Health check command
  - Environment variable configuration

#### Vector Memory (Semantic Search)
- **`sdk/vector_memory.py`** — Semantic memory with embeddings
  - Automatic text embedding (pluggable: use OpenAI, sentence-transformers, etc.)
  - Cosine similarity search
  - Metadata storage
  - Namespace isolation
  - `SemanticMemoryMixin` for easy agent integration
- **Vector Memory API endpoints** — 2 new HTTP endpoints:
  - `POST /api/v1/vector-memory/{namespace}/{key}` — Store text with embedding
  - `GET /api/v1/vector-memory/search?q={query}` — Semantic search
- **`tests/test_vector_memory.py`** — 12 tests for vector functionality
- **`docs/VECTOR_MEMORY.md`** — Semantic memory documentation

#### OpenClaw Integration
- **`docs/OPENCLAW_INTEGRATION.md`** — Complete integration guide
  - ObscuraClient class for OpenClaw
  - Spawn agents from chat
  - Shared memory access
  - Multi-agent workflows
  - System prompt templates

#### Agent Swarm Example
- **`examples/swarm_code_review.py`** — Multi-agent workflow demo
  - CodeAnalyzer → TestGenerator → DocWriter → Aggregator
  - Agent coordination via message passing
  - Shared memory for intermediate results
  - Real-world PR review simulation

#### Agent Runtime (Build Agents on Obscura)
- **`sdk/agents.py`** — Full agent lifecycle management
  - `AgentRuntime` — Spawn and manage multiple agents
  - `Agent` — Individual agent with state machine (PENDING → RUNNING → COMPLETED/FAILED)
  - Automatic memory integration — agents load/save context from shared memory
  - Message passing — agents communicate via async message bus
  - State persistence — agent status survives server restarts
  - Support for agent hierarchies (parent/child relationships)
- **Agent API endpoints** — 5 new HTTP endpoints:
  - `POST /api/v1/agents` — Spawn new agent
  - `GET /api/v1/agents/{id}` — Get agent status
  - `POST /api/v1/agents/{id}/run` — Run task on agent
  - `DELETE /api/v1/agents/{id}` — Stop and cleanup agent
  - `GET /api/v1/agents` — List all agents (with status filter)
- **`tests/test_agents.py`** — 16 tests for agent functionality
- **`docs/AGENTS.md`** — Complete agent runtime documentation

#### Shared Memory System (Multi-Tenant Agent Memory)
- **`sdk/memory.py`** — New module for auth-scoped key-value storage
  - Per-user SQLite databases (isolated by JWT `user_id`)
  - Namespaced storage (`session`, `user`, `project`, etc.)
  - Optional TTL for ephemeral data
  - Text search across keys and values
  - Thread-safe with singleton pattern per user
  - Global memory store for shared organization knowledge
- **Memory API endpoints** — 6 new HTTP endpoints:
  - `GET /api/v1/memory/{namespace}/{key}` — Retrieve value
  - `POST /api/v1/memory/{namespace}/{key}` — Store value (with optional TTL)
  - `DELETE /api/v1/memory/{namespace}/{key}` — Delete value
  - `GET /api/v1/memory` — List all keys
  - `GET /api/v1/memory/search?q={query}` — Search memory
  - `GET /api/v1/memory/stats` — Usage statistics
- **`tests/test_memory.py`** — 16 comprehensive tests for memory functionality
- **`docs/MEMORY.md`** — Full documentation for the memory system

### Fixed

#### Test Suite Fixes (Phase 1)
- **`test_send_success`** — Updated `ContentBlock` instantiation to use `kind=` instead of `type=` (matches dataclass field name)
- **`test_send_empty_prompt_rejected`** — Added auth dependency override to bypass JWT middleware during validation testing
- **`test_histogram_records_value`** — Fixed MeterProvider setup to handle already-initialized OTel providers gracefully
- **`test_traced_creates_span`** — Corrected `InMemorySpanExporter` import path for newer opentelemetry-sdk versions

### Changed

- Added `docs/TESTING.md` with comprehensive testing guide, common patterns, and troubleshooting

---

## Template

### Added
- New features

### Changed
- Changes to existing functionality

### Deprecated
- Soon-to-be removed features

### Removed
- Removed features

### Fixed
- Bug fixes

### Security
- Security improvements
