"""Default $skill templates scaffolded by ``/init``."""

from __future__ import annotations

PYTHON = """\
---
name: python
description: Python best practices, idioms, and type safety conventions
---

# Python Context

## Style & Conventions
- Use `from __future__ import annotations` at the top of every module
- Use type hints on all function signatures and return types
- Prefer `pathlib.Path` over `os.path`
- Prefer f-strings over `.format()` or `%`
- Use `asyncio` patterns for I/O-bound work
- Use `@dataclass(frozen=True)` for immutable value objects
- Use `Pydantic BaseModel` with `model_config = {"extra": "forbid"}` for input validation

## Error Handling
- Use specific exception types, not bare `except`
- Raise `ValueError` for invalid arguments, `TypeError` for wrong types
- Use `logging` module, not `print()` for diagnostics

## Testing
- Use `pytest` (not unittest)
- Name tests: `test_<what>_<condition>_<expected>`
- Don't mock what you can construct directly
- Use `tmp_path` fixture for file system tests

## Imports
- Standard library first, then third-party, then local
- Use absolute imports, not relative
- Avoid wildcard imports (`from x import *`)
"""

SECURITY = """\
---
name: security
description: Security review context — OWASP top 10, input validation, secrets handling
---

# Security Context

## Input Validation
- Never trust user input — validate at system boundaries
- Use allowlists over denylists for input validation
- Sanitize file paths to prevent directory traversal
- Validate URL schemes (reject `javascript:`, `data:`, `file:`)

## Secrets & Credentials
- Never hardcode secrets, tokens, or API keys
- Use environment variables or secret managers
- Never log secrets, even at DEBUG level
- Check for secrets in diffs before committing

## Common Vulnerabilities
- **Injection**: Parameterize all queries (SQL, shell, LDAP)
- **XSS**: Escape output in HTML contexts
- **SSRF**: Validate and restrict outbound URLs
- **Path traversal**: Resolve and check paths against allowed roots
- **Deserialization**: Never unpickle untrusted data; prefer JSON

## Authentication & Authorization
- Verify auth on every request, not just the first
- Use constant-time comparison for tokens/hashes
- Implement proper session expiry and rotation
"""

API = """\
---
name: api
description: REST API design context — conventions, status codes, error handling
---

# API Design Context

## URL Conventions
- Use nouns, not verbs: `/users` not `/getUsers`
- Use plural resources: `/users/{id}` not `/user/{id}`
- Nest for relationships: `/users/{id}/orders`
- Use query params for filtering: `/users?role=admin`

## Status Codes
- 200 OK — successful GET/PUT/PATCH
- 201 Created — successful POST that creates a resource
- 204 No Content — successful DELETE
- 400 Bad Request — validation error (include details)
- 401 Unauthorized — missing or invalid auth
- 403 Forbidden — valid auth but insufficient permissions
- 404 Not Found — resource doesn't exist
- 409 Conflict — state conflict (duplicate, version mismatch)
- 422 Unprocessable Entity — semantically invalid request
- 500 Internal Server Error — unexpected server failure

## Error Response Format
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Human-readable description",
    "details": [{"field": "email", "issue": "invalid format"}]
  }
}
```

## Pagination
- Use cursor-based pagination for large datasets
- Include `next_cursor` and `has_more` in response
- Accept `limit` and `cursor` query params
"""

TESTING = """\
---
name: testing
description: Testing strategy context — unit, integration, patterns, fixtures
---

# Testing Context

## Test Organization
- Unit tests: `tests/unit/` — fast, no I/O, no network
- Integration tests: `tests/integration/` — real databases, services
- E2E tests: `tests/e2e/` — full stack, marked with `@pytest.mark.e2e`

## Patterns
- **Arrange-Act-Assert**: Clear separation in each test
- **One assertion per concept**: Test one behavior per test function
- **Test behavior, not implementation**: Tests should survive refactoring
- **Use factories**: Build test data with helpers, not raw constructors

## What to Test
- Happy path (normal operation)
- Edge cases (empty input, max values, boundary conditions)
- Error paths (invalid input, missing resources, timeouts)
- State transitions (before/after mutations)

## What NOT to Test
- Third-party library internals
- Simple getters/setters with no logic
- Implementation details that may change

## Fixtures
- Prefer `tmp_path` over manual temp directory management
- Use `monkeypatch` for environment variables
- Use `conftest.py` for shared fixtures
- Scope fixtures appropriately (function, class, module, session)
"""

ASYNC = """\
---
name: async
description: Async Python patterns — asyncio, concurrency, error handling
---

# Async Python Context

## Patterns
- Use `async def` for I/O-bound operations
- Use `asyncio.gather()` for concurrent independent operations
- Use `asyncio.TaskGroup()` (Python 3.11+) for structured concurrency
- Use `asyncio.Queue` for producer-consumer patterns

## Error Handling
- Always handle `asyncio.CancelledError` — don't suppress it
- Use `try/finally` to clean up resources in async functions
- `asyncio.gather(return_exceptions=True)` to collect all results

## Anti-patterns
- Never use `asyncio.sleep()` as a synchronization mechanism
- Never call blocking I/O from async code (use `run_in_executor`)
- Avoid creating tasks without awaiting or storing references
- Don't mix `threading` and `asyncio` unless absolutely necessary

## Testing
- Use `asyncio_mode = "auto"` in pytest config
- Async fixtures work automatically with pytest-asyncio
- Use `asyncio.Event` or `asyncio.Condition` for test synchronization
"""

GIT = """\
---
name: git
description: Git workflow context — branching, commits, PR conventions
---

# Git Context

## Commit Messages
- First line: imperative mood, under 72 chars ("Add feature" not "Added feature")
- Blank line after subject
- Body: explain what and why, not how

## Branching
- `main` — production-ready, always deployable
- `feature/*` — new features branched from main
- `fix/*` — bug fixes
- `refactor/*` — code improvements with no behavior change

## PR Conventions
- Title: short, descriptive (under 70 chars)
- Body: summary bullets, test plan, link to issue
- Keep PRs focused — one concern per PR
- Request review when CI passes

## Safety
- Never force-push to main/master
- Never commit secrets, credentials, or .env files
- Always run tests before pushing
- Use `git stash` to save work in progress, not throwaway commits
"""

# Map of (directory_name, filename) -> content
OBSCURA = """\
---
name: obscura
description: Obscura system capabilities — architecture, tools, plugins, agents, specs, and integrations
---

# Obscura System Capabilities

Obscura is a multi-backend AI agent runtime. This skill provides context about what the system can do.

## Architecture

```
YAML Specs → Compiler → Frozen CompiledWorkspace → AgentLoop → Events → EventStore
                                                       ↑
                                                  HookRegistry
                                                  ToolBroker
```

## LLM Backends (6)

| Backend | Provider | Flag |
|---|---|---|
| Copilot | GitHub Copilot | `-b copilot` (default) |
| Claude | Anthropic Claude | `-b claude` |
| OpenAI | OpenAI GPT | `-b openai` |
| Codex | GitHub Codex | `-b codex` |
| LocalLLM | Ollama / LocalAI | `-b localllm` |
| Moonshot | Moonshot AI | `-b moonshot` |

## System Tools (35+)

**Code Execution:** `run_python3`, `run_npx`, `run_command`, `run_shell`
**File I/O:** `read_text_file`, `write_text_file`, `edit_text_file`, `append_text_file`, `list_directory`, `tree_directory`, `make_directory`, `remove_path`, `copy_path`, `move_path`, `file_info`, `download_file`
**Search:** `grep_files`, `find_files`, `diff_files`
**Git:** `git_status`, `git_diff`, `git_log`, `git_commit`, `git_branch`
**Web:** `web_fetch`, `web_search`, `http_request`
**System:** `get_environment`, `get_system_info`, `list_processes`, `signal_process`, `list_listening_ports`, `which_command`
**Data:** `json_query`, `clipboard_read`, `clipboard_write`
**Agent:** `delegate_to_agent`, `context_snapshot`, `policy_probe`, `todo_write`, `ask_user`

Tools have ~100 aliases for cross-LLM compatibility (e.g., `bash` → `run_command`, `cat` → `read_text_file`).

## Plugin System (48+ builtins)

Plugins are TOML manifests in `~/.obscura/plugins/` or `obscura/plugins/builtins/`. Each declares capabilities, tools, and bootstrap dependencies.

**Categories:** Data APIs (alphavantage, coingecko, sec-edgar), Cloud (kubernetes, docker, datadog, prometheus), Dev tools (ripgrep, fd, jq, duckdb, playwright, gitleaks), Messaging (matrix, nats), Microsoft 365 (msgraph), Search (websearch, shodan, censys).

## Agent System

- Spawn named agents with custom system prompts and memory namespaces
- Agent types: `loop` (interactive) and `daemon` (background service)
- Fleet management: spawn/coordinate multiple agents
- Inline invocation: `@agent_name <prompt>`
- Delegation: `delegate_to_agent()` tool for inter-agent communication
- Agent discovery from `~/.obscura/agents.yaml`

## YAML Spec System

Kubernetes-like envelope: `apiVersion: obscura/v1`, `kind`, `metadata`, `spec`.

| Kind | Purpose |
|---|---|
| `Template` | Reusable agent blueprint (model, tools, instructions, plugins, capabilities) |
| `Agent` | Concrete agent binding (template + input variables) |
| `Policy` | Trust rules (tool allow/deny, approval gates, audit) |
| `Pack` | Capability bundle (group of tools + permissions) |
| `Workspace` | Top-level bundle (templates + agents + policies + memory) |

Templates support inheritance (`extends`), tool allow/deny lists, MCP server configs, and input schemas.

## Event System & Hooks

**Event kinds:** TURN_START, TEXT_DELTA, THINKING_DELTA, TOOL_CALL, TOOL_RESULT, CONFIRMATION_REQUEST, TURN_COMPLETE, AGENT_DONE, ERROR, SESSION_PAUSED, USER_INPUT, CONTEXT_COMPACT

**Hooks:**
- `@hooks.before(kind)` — modify or suppress events before processing
- `@hooks.after(kind)` — observe events (side-effects only)
- Wildcard hooks fire on all events
- Hook factories in `obscura/core/lifecycle.py`: policy_gate, audit, redact, preflight, memory_inject

## Memory Systems

**Key-Value Store:** Per-user, namespaced, TTL-aware, SQLite-backed. Operations: set, get, delete, list_keys, search, clear.

**Vector Memory:** Semantic search with embeddings. Backends: Qdrant (cloud) or SQLite (local). Auto-saves conversation turns. Reranking with recency weighting.

## Integrations

| Integration | Protocol | Purpose |
|---|---|---|
| MCP | stdio/HTTP/SSE/gRPC | Connect to external tool servers |
| A2A | gRPC/HTTP | Agent-to-agent communication |
| Microsoft Graph | OAuth 2.0 | M365/Azure resources |
| iMessage | macOS native | Read/send iMessages |

## CLI Commands (44)

**Session:** /session, /agent, /skill, /delegate, /fleet, /swarm, /attention
**Execution:** /plan, /approve, /reject, /mode, /diff
**Discovery:** /discover, /mcp, /plugin, /capability, /pack, /inspect
**Context:** /context, /thinking, /compact, /memory, /audit
**Config:** /backend, /model, /tools, /confirm, /init
**System:** /help, /clear, /status, /health, /running, /kill, /broker, /policies

## TUI Modes

| Mode | Purpose |
|---|---|
| ASK | Interactive Q&A (default) |
| PLAN | Structured planning with step approval |
| CODE | Code generation and editing |
| DIFF | Diff review and application |

## REPL Prefixes

| Prefix | Purpose | Example |
|---|---|---|
| `/command` | Built-in REPL command | `/help`, `/backend claude` |
| `$skill` | Load context injection | `$python`, `$security` |
| `@command [args]` | Run prompt template | `@review file.py` |
| `*@command` | Benchmark with eval suite | `*@review` |

Chain: `$skill $skill @command args`

## Configuration

### config.toml

Located at `.obscura/config.toml` (project) or `~/.obscura/config.toml` (global).

```toml
mode = "code"  # "code" (all tools) | "ask" (no tools) | "plan" (read-only) | "diff" (read + git)

[plugins]
load_builtins = true

[plugins.bootstrap]
auto_install = true
lenient_builtins = true

[defaults.capabilities]
grant = ["shell.exec", "file.read", "file.write", "git.ops", "web.browse", "search.web", "security.scan"]
deny = []

[mcp]
auto_discover = true
```

### settings.json

Project hooks in `.obscura/settings.json`:

```json
{
  "hooks": {
    "preToolUse": [{ "bash": "my-linter --check", "matcher": "run_shell" }]
  }
}
```

### Environment Variables

| Variable | Purpose |
|---|---|
| `OBSCURA_HOME` | Override home directory (default `~/.obscura/`) |
| `OBSCURA_SYSTEM_TOOLS_ALLOWED_COMMANDS` | Whitelist shell commands |
| `OBSCURA_SYSTEM_TOOLS_DENIED_COMMANDS` | Blacklist shell commands |
| `OBSCURA_SYSTEM_TOOLS_BASE_DIR` | Restrict file operations to a directory |
| `OBSCURA_SYSTEM_TOOLS_UNSAFE_FULL_ACCESS` | Disable all restrictions (dangerous) |

## Capabilities & Policies

### Capability Model

Capabilities group tools into grantable permissions. Each capability has:
- `id` — e.g., `shell.exec`, `file.read`, `dev.jq`
- `tools` — list of tools belonging to this capability
- `requires_approval` — whether user must confirm each use
- `default_grant` — whether auto-granted to new agents

Default grants: `shell.exec`, `file.read`, `file.write`, `git.ops`, `web.browse`, `search.web`, `security.scan`.

### Policy Rules

Policies in `~/.obscura/policies/*.toml` control tool access:

```toml
[[rules]]
id = "deny-shell"
tool = "shell_exec"
action = "deny"        # allow | deny | approve
reason = "no shells"
priority = 10
```

Rules match on: `plugin`, `trust_level`, `capability`, `tool`, `agent`, `environment`. Higher priority rules are evaluated first.

**Trust levels:** `builtin` (auto-allow), `verified` (auto-allow), `community` (auto-allow), `untrusted` (auto-deny).

### Tool Broker Pipeline

All tool execution goes through the ToolBroker:

```
Tool call → Policy check → Capability check → Approval gate → Execute → Audit
```

The broker logs every execution with action, latency, matched rule, and errors.

## Path Layout

```
~/.obscura/
├── agents.yaml          # Agent definitions
├── config.toml          # Runtime config
├── commands/            # @command markdown files
├── skills/              # $skill markdown files
├── evals/               # *eval test suites
├── specs/               # YAML specs (templates, policies, workspaces, packs)
├── plugins/             # Plugin overrides
├── mcp/                 # MCP server configs
├── hooks/               # Lifecycle hooks
├── memory/              # Key-value stores
├── sessions/            # Agent session data
├── state/               # Runtime state
├── output/              # Agent-generated files
└── events.db            # Event store (SQLite)
```

Project-local `.obscura/` overrides global `~/.obscura/` (merge order: global first, local wins).

## Supervisor

Single-writer coordinator with SQLite advisory locks. Guarantees one active run per session. Full event sourcing for replay. State machine: RUNNING → WAITING → PAUSED → COMPLETED/FAILED.
"""

DEFAULT_SKILLS: dict[str, str] = {
    "python": PYTHON,
    "security": SECURITY,
    "api": API,
    "testing": TESTING,
    "async": ASYNC,
    "git": GIT,
    "obscura": OBSCURA,
}
