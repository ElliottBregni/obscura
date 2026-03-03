```
  ###   ####   ####   ####  #   #  ####     #
 #   #  #   #  #      #     #   #  #   #   # #
 #   #  ####    ###    ###  #   #  ####   #####
 #   #  #   #      #      # #   #  #  #   #   #
  ###   ####   ####   ####   ###   #   #  #   #
```

# Obscura

Multi-backend AI agent runtime with 53 tools, vector memory, multi-agent orchestration, and MCP integration.

## Install

```bash
git clone <repo-url>
cd obscura
uv sync
```

## CLI Usage

```bash
# Interactive REPL (default backend: copilot)
obscura

# Choose backend
obscura -b claude
obscura -b copilot
obscura -b codex

# Choose model
obscura -b claude -m claude-sonnet-4-5-20250929

# Single-shot prompt
obscura "explain this code"
obscura -b claude -p "summarize this file"
```

### Slash Commands

| Command | Args | Description |
|---------|------|-------------|
| `/help` | | Show available commands |
| `/backend` | `copilot\|claude\|codex` | Switch LLM backend |
| `/model` | `<model-id>` | Switch model |
| `/system` | `<prompt>` | Set system prompt |
| `/tools` | `on\|off` | Toggle tool execution |
| `/confirm` | `on\|off` | Toggle tool approval prompts |
| `/mode` | `ask\|plan\|code` | Switch interaction mode |
| `/plan` | | Show current plan |
| `/approve` | `[all]` | Approve plan step(s) |
| `/reject` | `[all]` | Reject plan step(s) |
| `/diff` | `accept\|reject\|apply` | Review file changes |
| `/context` | | Show context window usage |
| `/compact` | `[n]` | Summarize history to free tokens |
| `/agent` | `spawn\|list\|stop\|run` | Manage agents |
| `/fleet` | `spawn\|status\|run\|delegate\|stop` | Multi-agent fleet |
| `/attention` | `respond` | Respond to agent attention requests |
| `/session` | `list\|new` | Session management |
| `/discover` | `web\|filesystem\|git\|database\|ai\|cloud\|search` | Discover tools/services |
| `/mcp` | `discover\|list\|select\|env\|install` | MCP server management |
| `/memory` | `stats\|search\|clear` | Vector memory operations |
| `/quit` | | Exit the REPL |

## Tools (53 Total)

Obscura exposes 49 system tools + 4 memory tools to the LLM during every session.

### Execution (5)

| Tool | Description |
|------|-------------|
| `run_python3` | Execute Python 3 code |
| `run_python` | Execute Python code (alias) |
| `run_npx` | Run npx commands |
| `run_command` | Run shell commands with safety checks |
| `run_shell` | Execute arbitrary shell commands |

### Web (2)

| Tool | Description |
|------|-------------|
| `web_fetch` | Fetch and parse web pages |
| `web_search` | Search the web |

### Filesystem (12)

| Tool | Description |
|------|-------------|
| `list_directory` | List files in a directory |
| `read_text_file` | Read file contents |
| `write_text_file` | Write/create files |
| `append_text_file` | Append to existing files |
| `make_directory` | Create directories |
| `remove_path` | Remove files/directories |
| `grep_files` | Search file contents with regex |
| `find_files` | Find files by pattern |
| `edit_text_file` | Edit files with search/replace |
| `copy_path` | Copy files/directories |
| `move_path` | Move/rename files |
| `file_info` | Get file metadata |

### Git (5)

| Tool | Description |
|------|-------------|
| `git_status` | Show working tree status |
| `git_diff` | Show file diffs |
| `git_log` | Show commit history |
| `git_commit` | Create commits |
| `git_branch` | Branch management |

### Utilities (7)

| Tool | Description |
|------|-------------|
| `tree_directory` | Show directory tree |
| `diff_files` | Diff two files |
| `download_file` | Download files from URLs |
| `http_request` | Make HTTP requests |
| `clipboard_read` | Read from clipboard |
| `clipboard_write` | Write to clipboard |
| `json_query` | Query JSON with jq-like syntax |

### System Info (7)

| Tool | Description |
|------|-------------|
| `get_environment` | Show environment variables |
| `get_system_info` | System information |
| `list_processes` | List running processes |
| `signal_process` | Send signals to processes |
| `list_listening_ports` | Show listening ports |
| `security_lookup` | Security-related lookups |
| `manage_crontab` | Manage cron jobs |

### Dynamic Tools & Sandbox (4)

| Tool | Description |
|------|-------------|
| `create_tool` | Create new tools at runtime |
| `call_dynamic_tool` | Call dynamically created tools |
| `list_dynamic_tools` | List all dynamic tools |
| `code_sandbox` | Execute code in isolated sandbox |

### Context & Discovery (4)

| Tool | Description |
|------|-------------|
| `context_window_status` | Check token usage and auto-compact thresholds |
| `list_system_tools` | List all available tools |
| `list_unix_capabilities` | List Unix capabilities |
| `task` | Delegate subtasks to sub-agents |

### Copilot Bridge (1)

| Tool | Description |
|------|-------------|
| `copilot_query` | Query GitHub Copilot with GPT-5 Mini (hardcoded model) |

### Memory Tools (4)

These are added from `make_memory_tool_specs()` and bound to the authenticated user's vector store:

| Tool | Description |
|------|-------------|
| `store_memory` | Store a key-value memory |
| `recall_memory` | Recall a memory by key |
| `semantic_search` | Search memories by meaning |
| `store_searchable` | Store a memory with embedding for semantic search |

### Tool Aliases

Common aliases resolve to canonical tool names:

```
remember        → store_searchable
save_memory     → store_searchable
search_memory   → semantic_search
memory_search   → semantic_search
copilot         → copilot_query
gpt5            → copilot_query
ask_copilot     → copilot_query
ls              → list_directory
cat             → read_text_file
grep            → grep_files
find            → find_files
```

Plus ~100 more aliases (see `obscura/core/tools.py`).

## Agent Manifests

Agents are defined as `*.agent.md` files with YAML frontmatter:

```markdown
---
name: reviewer
provider: claude
model_id: claude-sonnet-4-5-20250929
tools:
  - read_text_file
  - grep_files
  - git_diff
can_delegate: true
delegate_allowlist:
  - writer
max_turns: 25
agent_type: loop
tags:
  - code-review
permissions:
  allow:
    - "read_*"
    - "grep_*"
  deny:
    - "remove_path"
    - "run_shell"
---

You are a code reviewer. Analyze code changes for bugs,
style issues, and security concerns. Be thorough but concise.
```

### Manifest Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | required | Agent name |
| `provider` | string | `copilot` | LLM backend (`copilot`, `claude`, `openai`, `localllm`, `moonshot`) |
| `model_id` | string | null | Specific model to use |
| `tools` | list | `[]` | Tools the agent can use |
| `tool_allowlist` | list | null | Restrict to only these tools |
| `permissions.allow` | list | `[]` | Glob patterns for allowed tools |
| `permissions.deny` | list | `[]` | Glob patterns for denied tools |
| `can_delegate` | bool | `false` | Whether agent can delegate to others |
| `delegate_allowlist` | list | `[]` | Agents this agent can delegate to |
| `max_delegation_depth` | int | `3` | Max delegation chain depth |
| `agent_type` | string | `loop` | Agent execution type |
| `max_turns` | int | `25` | Max conversation turns |
| `tags` | list | `[]` | Metadata tags |

### Skill Manifests

Skills are loaded from `~/.claude/skills/` (or `~/.github/skills/` for Copilot):

```markdown
---
name: deploy
description: Deploy application to production
user-invocable: true
allowed-tools:
  - run_command
  - run_shell
---

Follow these deployment steps:
1. Run tests
2. Build the application
3. Deploy to staging
4. Run smoke tests
5. Promote to production
```

### Instruction Manifests

Context-specific instructions from `~/.claude/instructions/`:

```markdown
---
applyTo: "*.py"
---

When working with Python files:
- Use type hints everywhere
- Follow PEP 8
- Add docstrings to public functions
```

### MCP Server Configuration

MCP servers are configured in `~/.obscura/mcp/servers.json` or `.mcp.json`:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@anthropic-ai/mcp-filesystem"],
      "transport": "stdio"
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@anthropic-ai/mcp-github"],
      "env": {
        "GITHUB_TOKEN": "${GITHUB_TOKEN}"
      }
    }
  }
}
```

### Hooks

Lifecycle hooks in `hooks.json`:

```json
{
  "hooks": {
    "preToolUse": [
      {
        "type": "command",
        "command": "echo 'Tool about to be used'",
        "timeout": 10
      }
    ],
    "postToolUse": [
      {
        "type": "command",
        "command": "echo 'Tool completed'"
      }
    ]
  }
}
```

## Session Storage

Sessions are event-sourced and persisted to SQLite at `~/.obscura/events.db`.

### Session Lifecycle

```
RUNNING → WAITING_FOR_TOOL → RUNNING
RUNNING → WAITING_FOR_USER → RUNNING
RUNNING → PAUSED → RUNNING
RUNNING → COMPLETED
RUNNING → FAILED
```

### Session Commands

```bash
# List recent sessions
/session list

# Start a new session (preserves the old one)
/session new

# Sessions are auto-created on REPL start
# Session ID is shown in the banner
```

### Event Types

Every interaction is stored as an immutable event:

| Event | Description |
|-------|-------------|
| `user_message` | User input |
| `text_delta` | Streamed assistant response chunks |
| `tool_call` | Tool invocation with arguments |
| `tool_result` | Tool execution result |
| `turn_start` | Beginning of a conversation turn |
| `turn_complete` | End of a conversation turn |
| `action` | Agent action events |

### Storage Location

```
~/.obscura/
  events.db          # Session event store (SQLite)
  events.db-wal      # Write-ahead log
  memory/            # Per-user key-value memory
  vector_memory/     # Semantic vector store
  mcp/               # MCP server configs
  logs/              # Trace logs (JSONL)
```

## Vector Memory

Vector memory is wired into the CLI lifecycle automatically:

1. **Session start** -- Loads recent memories into system prompt context
2. **Pre-message** -- Searches for relevant memories before each user message (RAG)
3. **Post-message** -- Auto-saves conversation turns in background threads
4. **Explicit tools** -- LLM can call `store_memory`, `recall_memory`, `semantic_search`, `store_searchable`

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OBSCURA_VECTOR_MEMORY` | `on` | Enable/disable vector memory (`off` to disable) |
| `QDRANT_URL` | -- | Qdrant server URL (falls back to SQLite) |
| `QDRANT_API_KEY` | -- | Qdrant API key |

### Memory Commands

```bash
/memory stats     # Show vector store statistics
/memory search <query>  # Search memories semantically
/memory clear     # Clear all CLI conversation memories
```

## Configuration

### Auth Credentials

| Backend | Environment Variables |
|---------|----------------------|
| Copilot | `GITHUB_TOKEN`, `GH_TOKEN`, or `gh auth token` |
| Claude | `ANTHROPIC_API_KEY`, `CLAUDE_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Moonshot | `MOONSHOT_API_KEY` |
| LocalLLM | `OBSCURA_LOCALLLM_BASE_URL` (default: `http://localhost:1234/v1`) |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OBSCURA_AUTH_ENABLED` | `true` | Enable JWT authentication |
| `OBSCURA_PORT` | `8080` | Server port |
| `OBSCURA_MEMORY_DIR` | `~/.obscura/memory` | Memory storage path |
| `OBSCURA_LOG_LEVEL` | `INFO` | Logging level |
| `OBSCURA_VECTOR_MEMORY` | `on` | Vector memory toggle |
| `OBSCURA_OUTPUT_MODE` | `plain` | Output mode (`json` or `plain`) |
| `OBSCURA_SYSTEM_TOOLS_UNSAFE_FULL_ACCESS` | `false` | Bypass tool safety checks |
| `OBSCURA_SYSTEM_TOOLS_BASE_DIR` | -- | Restrict filesystem tools to this directory |
| `OBSCURA_SYSTEM_TOOLS_ALLOWED_COMMANDS` | -- | Comma-separated allowed shell commands |
| `OBSCURA_SYSTEM_TOOLS_DENIED_COMMANDS` | `rm,sudo,shutdown,...` | Comma-separated denied shell commands |

## Development

```bash
# Unit tests
pytest tests/ -v -m "not e2e"

# With coverage
pytest tests/ --cov=obscura --cov-report=term-missing

# Type checking
pyright

# Linting
ruff check .
ruff format --check .
```

## License

MIT
