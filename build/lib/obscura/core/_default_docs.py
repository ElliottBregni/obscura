"""Default documentation files generated during workspace init."""

from __future__ import annotations

PLUGIN_GUIDE = """\
# Creating an Obscura Plugin

## Quick Start

1. Create a TOML manifest file
2. Place it in `~/.obscura/plugins/` (global) or `.obscura/plugins/` (project-local)
3. Obscura auto-discovers it on next run

## File Placement

```
# Option A: Flat file
~/.obscura/plugins/my-plugin.toml

# Option B: Subdirectory (for plugins with multiple files)
~/.obscura/plugins/my-plugin/plugin.toml

# Option C: Project-local (scoped to one project)
.obscura/plugins/my-plugin/plugin.toml
```

TOML is the preferred format. YAML is deprecated.

## Minimum Viable Plugin

```toml
id = "my-plugin"
name = "My Plugin"
version = "1.0.0"
source_type = "local"
runtime_type = "native"
description = "What this plugin does."

[[capabilities]]
id = "my.capability"
description = "What this capability grants"
tools = ["my_tool"]

[[tools]]
name = "my_tool"
description = "What the tool does"
capability = "my.capability"
handler = "my_package.module:my_function"
```

## Manifest Fields

### Required

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique ID, kebab-case (`^[a-z][a-z0-9_-]*$`) |
| `name` | string | Human-readable name |
| `version` | string | Semantic version (e.g., `"1.0.0"`) |
| `source_type` | string | `local`, `git`, `pip`, `builtin`, `npm`, `cargo`, `uv`, `registry` |
| `runtime_type` | string | `native`, `cli`, `sdk`, `mcp`, `service`, `content`, `npx`, `wasm`, `docker`, `grpc` |

### Optional

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `author` | string | `""` | Author name |
| `description` | string | `""` | Plugin description |
| `trust_level` | string | `"community"` | `builtin`, `verified`, `community`, `untrusted` |

## Capabilities

Capabilities are named permission surfaces that group tools together.
All array sections support both list syntax (`[[section]]`) and dict syntax (`[section.key]`).

```toml
# List syntax (preferred for multiple entries)
[[capabilities]]
id = "repo.read"                   # dot-separated ID
description = "Read repository contents"
tools = ["search_repo", "get_file"] # tools gated by this capability
requires_approval = false           # user must approve each call (default: false)
default_grant = true                # granted by default (default: true)
```

```toml
# Dict syntax (alternative)
[capabilities.repo_read]
id = "repo.read"
description = "Read repository contents"
tools = ["search_repo", "get_file"]
```

## Tools

Tools are the functions your plugin exposes to the agent.

```toml
[[tools]]
name = "search_repo"               # unique name within plugin
description = "Search repository contents by keyword"
handler = "my_plugin.tools:search_repo"  # dotted.path:function
capability = "repo.read"           # which capability gates this tool
side_effects = "read"              # "none" | "read" | "write" (default: "none")
timeout_seconds = 60.0             # per-call timeout (default: 60.0)
retries = 0                        # retry count on failure (default: 0)

[tools.parameters]                 # JSON Schema for parameters
type = "object"
required = ["query"]
[tools.parameters.properties.query]
type = "string"
description = "Search query"
```

### Handler Format

The `handler` field is a dotted import path: `package.module:function_name`

```python
# my_plugin/tools.py

async def search_repo(query: str, limit: int = 10) -> str:
    \"\"\"Search repository contents.\"\"\"
    # Your implementation here
    return json.dumps(results)
```

Handlers can be sync or async. Sync handlers are automatically wrapped
with `asyncio.to_thread()`.

## Configuration Requirements

Declare environment variables your plugin needs:

```toml
[config.API_KEY]
type = "secret"                    # "string" | "int" | "bool" | "secret"
required = true
description = "API key for the service"

[config.BASE_URL]
type = "string"
required = false
default = "https://api.example.com"
description = "API base URL"
```

The plugin will be disabled if required config values are missing
(unless it's a builtin with lenient mode enabled).

## Bootstrap Dependencies

Declare dependencies that should be auto-installed:

```toml
[bootstrap]
deps = [
    { type = "pip", package = "requests", version = ">=2.0" },
    { type = "binary", package = "gh", optional = true },
    { type = "npm", package = "@scope/tool", optional = true },
    { type = "cargo", package = "my-cli", optional = true },
    { type = "brew", package = "jq", optional = true },
]
post_install = "echo 'Setup complete'"  # runs after all deps installed
check_command = "gh --version"          # verify bootstrap succeeded
```

### Dependency Types

| Type | Action | Target |
|------|--------|--------|
| `pip` | Install into `~/.obscura/venv/` | PyPI package |
| `uv` | Install via `uv pip install` | PyPI package |
| `npm` | `npm install -g` | npm package |
| `npx` | Verify npx available | npm package (on-demand) |
| `cargo` | `cargo install` | Rust crate |
| `binary` | Verify on PATH (no install) | System binary |
| `brew` | `brew install` | Homebrew formula |
| `pipx` | `pipx install` or `uv tool install` | Isolated CLI tool |

Set `optional = true` for deps that aren't strictly required.

## Healthcheck

Monitor plugin health at runtime:

```toml
[healthcheck]
type = "binary"                    # "callable" | "http" | "binary" | "python_import"
target = "gh"                      # binary name, URL, or dotted path
interval_seconds = 300             # check interval (default: 300)
```

## Instructions

Inject text into the agent's system prompt. Supports list and dict syntax.

```toml
# List syntax
[[instructions]]
id = "my-review-guide"
version = "1.0.0"
scope = "agent"                    # "global" | "agent" | "session"
content = "When reviewing code, always check for security issues."
priority = 50                      # lower = earlier in prompt (default: 50)
```

```toml
# Dict syntax (alternative)
[instructions.my-review-guide]
scope = "agent"
content = "When reviewing code, always check for security issues."
priority = 50
```

## Policy Hints

Recommend access rules for your capabilities. Supports list and dict syntax.

```toml
# List syntax
[[policy_hints]]
capability_id = "repo.write"
recommended_action = "approve"     # "allow" | "deny" | "approve"
reason = "Write operations should require user confirmation"
```

```toml
# Dict syntax (alternative — key becomes capability_id)
[policy_hints.repo_write]
capability_id = "repo.write"
recommended_action = "approve"
reason = "Write operations should require user confirmation"
```

## Workflows

Define multi-step behaviors. Supports list and dict syntax.

```toml
# List syntax
[[workflows]]
id = "review_pr"
name = "Review Pull Request"
version = "1.0.0"
description = "Automated PR review workflow"
required_capabilities = ["repo.read", "pr.comment"]
steps = [
    { tool = "get_file", description = "Fetch changed files" },
    { tool = "comment_pr", description = "Post review comment" },
]
```

```toml
# Dict syntax (alternative)
[workflows.review_pr]
name = "Review Pull Request"
description = "Automated PR review workflow"
required_capabilities = ["repo.read", "pr.comment"]
steps = [
    { tool = "get_file", description = "Fetch changed files" },
    { tool = "comment_pr", description = "Post review comment" },
]
```

## Syntax Note

All array sections (`[[capabilities]]`, `[[tools]]`, `[[instructions]]`,
`[[policy_hints]]`, `[[workflows]]`) support both TOML list syntax and dict
syntax. The dict key is used as the entry's primary ID field.

## Plugin Lifecycle

```
discovered → installed → enabled → active → unhealthy → disabled → failed
```

1. **Discover** — Manifest found in plugin directory
2. **Validate** — Fields checked (ID format, semver, capability refs)
3. **Check Config** — Required env vars verified
4. **Bootstrap** — Dependencies installed
5. **Register** — Tools registered with the broker
6. **Healthcheck** — Periodic liveness checks

## CLI Commands

```bash
obscura plugin list              # List all plugins with status
obscura plugin install <source>  # Install from path/git/pip
obscura plugin remove <id>       # Uninstall
obscura plugin enable <id>       # Enable a disabled plugin
obscura plugin disable <id>      # Disable without removing
obscura plugin info <id>         # Show manifest and contributions
obscura plugin health            # Health status overview
```

Or in the REPL: `/plugin list`, `/plugin install <source>`, etc.

## Full Example

```toml
id = "my-github"
name = "GitHub Tools"
version = "1.0.0"
author = "your-name"
source_type = "local"
runtime_type = "native"
trust_level = "community"
description = "GitHub repository tools for code search and PR review."

[config.GITHUB_TOKEN]
type = "secret"
required = true
description = "GitHub personal access token"

[[capabilities]]
id = "repo.read"
description = "Read repository contents"
tools = ["github_search", "github_get_file"]
requires_approval = false
default_grant = true

[[capabilities]]
id = "pr.comment"
description = "Comment on pull requests"
tools = ["github_comment_pr"]
requires_approval = true
default_grant = false

[[tools]]
name = "github_search"
description = "Search repository contents"
handler = "my_github.tools:search"
capability = "repo.read"
side_effects = "read"

[tools.parameters]
type = "object"
required = ["owner", "repo", "query"]
[tools.parameters.properties.owner]
type = "string"
description = "Repository owner"
[tools.parameters.properties.repo]
type = "string"
description = "Repository name"
[tools.parameters.properties.query]
type = "string"
description = "Search query"

[[tools]]
name = "github_get_file"
description = "Get file contents from a repository"
handler = "my_github.tools:get_file"
capability = "repo.read"
side_effects = "read"

[[tools]]
name = "github_comment_pr"
description = "Add a comment to a pull request"
handler = "my_github.tools:comment_pr"
capability = "pr.comment"
side_effects = "write"

[[workflows]]
id = "review_pr"
name = "Review Pull Request"
version = "1.0.0"
description = "Fetch changed files and post a review comment."
required_capabilities = ["repo.read", "pr.comment"]
steps = [
    { tool = "github_get_file", description = "Fetch each changed file" },
    { tool = "github_comment_pr", description = "Post review summary" },
]

[[instructions]]
id = "github-review-guide"
version = "1.0.0"
scope = "agent"
content = "When reviewing PRs, check for security issues, missing error handling, and test coverage."
priority = 50

[[policy_hints]]
capability_id = "pr.comment"
recommended_action = "approve"
reason = "Write operations require user confirmation"

[bootstrap]
deps = [
    { type = "binary", package = "gh", optional = true },
]
check_command = "gh --version"

[healthcheck]
type = "binary"
target = "gh"
interval_seconds = 300
```
"""


CONFIG_REFERENCE = """\
# Obscura TOML Configuration Reference

All Obscura configuration uses TOML format (preferred). YAML is deprecated.

---

## Table of Contents

1. [Workspace Spec](#1-workspace-spec)
2. [Template Spec](#2-template-spec)
3. [Policy Spec](#3-policy-spec)
4. [Pack Spec](#4-pack-spec)
5. [Plugin Manifest](#5-plugin-manifest)
6. [Workspace Config](#6-workspace-config)
7. [Policy Rules](#7-policy-rules)
8. [MCP Server Config](#8-mcp-server-config)
9. [Directory Structure](#9-directory-structure)
10. [Compilation Pipeline](#10-compilation-pipeline)

---

## Spec Envelope

All spec files (Workspace, Template, Policy, Pack) use a Kubernetes-like envelope:

```toml
apiVersion = "obscura/v1"
kind = "Workspace"        # or "Template", "Policy", "Pack"

[metadata]
name = "identifier"       # REQUIRED — lowercase, unique within kind
description = ""          # optional
tags = []                 # optional

[spec]
# kind-specific fields...
```

---

## 1. Workspace Spec

**Location**: `~/.obscura/specs/workspaces/*.toml` or `.obscura/specs/workspaces/*.toml`
**Purpose**: Top-level runtime entrypoint — bundles agents, policies, plugins, and memory.

```toml
apiVersion = "obscura/v1"
kind = "Workspace"

[metadata]
name = "my-workspace"           # REQUIRED
description = "Description"
tags = ["backend", "dev"]

[spec]
packs = ["pack-name"]           # Pack spec names to include (default: [])
policies = ["safe-dev"]         # Policy spec names to apply (default: [])
config = { key = "value" }      # Runtime config overrides (default: {})

[spec.plugins]
include = ["docker-engine"]     # Plugin allowlist (default: [] = all)
exclude = ["untrusted-plugin"]  # Plugin denylist (default: [])

[spec.memory]
namespace = "my-ns"             # REQUIRED if memory block present
shared_scope = "workspace"      # "workspace" | "agent" | "session" (default: "workspace")
stores = ["key-value", "vector"] # Store backend names (default: [])
retention_days = 30             # default: 30

# Use [[spec.agents]] for each agent (TOML array-of-tables)
[[spec.agents]]
name = "dev"                    # REQUIRED
template = "base-agent"         # REQUIRED — template spec name
mode = "task"                   # "task" | "daemon" | "reactive" | "scheduled" (default: "task")
input = { repo_path = "." }     # Input variables (default: {})
overrides = {}                  # Template field overrides (default: {})

[[spec.agents]]
name = "watcher"
template = "base-agent"
mode = "daemon"

[spec.startup]
preload_plugins = true          # default: true
start_agents = ["dev"]          # Agent names to auto-start (default: [])
```

---

## 2. Template Spec

**Location**: `~/.obscura/specs/templates/*.toml` or `.obscura/specs/templates/*.toml`
**Purpose**: Reusable agent blueprint. Templates can inherit from one parent via `extends`.

```toml
apiVersion = "obscura/v1"
kind = "Template"

[metadata]
name = "code-agent"
description = "Agent for code analysis"

[spec]
extends = "base-agent"         # Parent template name, max depth 1 (default: null)
agent_type = "loop"            # "loop" | "daemon" | "reactive" | "scheduled" (default: "loop")
max_iterations = 25            # default: 25
provider = "copilot"           # "copilot" | "claude" | "openai" | "localllm" | "moonshot" (default: "copilot")
model_id = "claude-sonnet-4-5-20250929"  # optional LLM model override (default: null)

instructions = "You are a senior developer."  # System prompt (default: "")

plugins = ["gitleaks", "ripgrep"]    # Plugin IDs to load (default: [])
capabilities = ["shell.exec", "git.ops"]  # Capabilities to enable (default: [])

tool_allowlist = ["read_file", "edit_file"]  # ONLY these tools allowed. null = all (default: null)
tool_denylist = ["dangerous_tool"]           # Tools explicitly denied (default: [])

config = { key = "value" }      # Arbitrary runtime config (default: {})
input_schema = {}               # JSON Schema for expected input vars (default: {})

# MCP server definitions (optional)
[[spec.mcp_servers]]
name = "filesystem"
transport = "stdio"             # "stdio" | "sse" | "http" (default: "stdio")
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/path"]
env = { KEY = "value" }
```

### Template Inheritance

Lists are **unioned** (deduplicated). Dicts are **deep merged** (child wins on conflict).

```toml
# base-agent: plugins = ["system-tools", "websearch"]
# code-agent (extends = "base-agent"): plugins = ["gitleaks"]
# Result: plugins = ["system-tools", "websearch", "gitleaks"]
```

---

## 3. Policy Spec

**Location**: `~/.obscura/specs/policies/*.toml` or `.obscura/specs/policies/*.toml`
**Purpose**: Trust and execution constraints applied to a workspace.

```toml
apiVersion = "obscura/v1"
kind = "Policy"

[metadata]
name = "safe-dev"
description = "Conservative development policy"

[spec]
tool_allowlist = ["read_file"]     # ONLY these tools. null = all (default: null)
tool_denylist = ["rm_rf"]          # Tools denied (default: [])
require_confirmation = ["bash", "write_file"]  # Tools needing user approval (default: [])

plugin_allowlist = ["system-tools"]  # ONLY these plugins. null = all (default: null)
plugin_denylist = []                 # Plugins denied (default: [])

max_turns = 25                  # Max agent iterations (default: 25)
token_budget = 0                # Token limit, 0 = unlimited (default: 0)
base_dir = "/home/user/project" # Filesystem root restriction (default: null)
allow_dynamic_tools = false     # Runtime tool registration (default: false)
```

---

## 4. Pack Spec

**Location**: `~/.obscura/specs/packs/*.toml` or `.obscura/specs/packs/*.toml`
**Purpose**: Curated bundle of plugins, templates, policies, and config.

```toml
apiVersion = "obscura/v1"
kind = "Pack"

[metadata]
name = "code-quality-pack"
description = "Development quality tools"
tags = ["dev", "quality"]

[spec]
plugins = ["system-tools", "gitleaks"]  # Plugin IDs (default: [])
templates = ["code-agent"]              # Recommended templates (default: [])
policies = ["safe-dev"]                 # Policies to apply (default: [])
instructions = "Always scan for secrets before committing."  # Prompt overlay (default: "")
config = { key = "value" }             # Config defaults (default: {})

[spec.capabilities]
grant = ["shell.exec", "file.read"]    # Capabilities to enable (default: [])
deny = ["finance.quotes"]              # Capabilities to disable (default: [])
```

---

## 5. Plugin Manifest

**Location**:
- Builtins: `obscura/plugins/builtins/<id>.toml` (shipped with Obscura)
- User: `~/.obscura/plugins/<id>.toml` or `~/.obscura/plugins/<id>/plugin.toml`
- Project: `.obscura/plugins/<id>.toml` or `.obscura/plugins/<id>/plugin.toml`

**Purpose**: Declares a plugin's tools, capabilities, dependencies, and health checks.
**Note**: Plugin manifests do NOT use the Kubernetes envelope — they are flat TOML.
**Syntax**: All array sections (`capabilities`, `tools`, `instructions`, `policy_hints`,
`workflows`) support both TOML list syntax (`[[section]]`) and dict syntax (`[section.key]`).
The dict key is used as the entry's primary ID field.

### Required Fields

```toml
id = "my-plugin"              # REQUIRED — kebab-case (^[a-z][a-z0-9_-]*$)
name = "My Plugin"            # REQUIRED — human-readable
version = "1.0.0"             # REQUIRED — semver
source_type = "local"         # REQUIRED — "local" | "git" | "pip" | "builtin" | "npm" | "cargo" | "uv" | "registry"
runtime_type = "native"       # REQUIRED — "native" | "cli" | "sdk" | "mcp" | "service" | "content" | "npx" | "wasm" | "docker" | "grpc"
```

### Optional Fields

```toml
author = ""                   # default: ""
description = ""              # default: ""
trust_level = "community"     # "builtin" | "verified" | "community" | "untrusted" (default: "community")
```

### Configuration Requirements

Declare environment variables your plugin needs. Two syntax styles:

```toml
# Dict style (preferred) — key name becomes the env var name
[config.GITHUB_TOKEN]
type = "secret"               # "string" | "int" | "bool" | "secret" (default: "string")
required = true               # default: true
description = "GitHub PAT"
default = ""                  # default: null (no default = must be set)

[config.BASE_URL]
type = "string"
required = false
default = "https://api.example.com"

# List style (alternative)
# [[config]]
# key = "API_KEY"
# type = "secret"
# required = true
```

### Capabilities

```toml
[[capabilities]]
id = "repo.read"              # REQUIRED — dot-separated
description = "Read repositories"
tools = ["search_repo", "get_file"]  # Tools gated by this capability
requires_approval = false     # User must approve each call (default: false)
default_grant = true          # Granted by default (default: true)
# version = "1.0.0"          # Defaults to "1.0.0" if omitted
```

### Tools

```toml
[[tools]]
name = "search_repo"          # REQUIRED — unique within plugin
description = "Search repo contents"
handler = "my_plugin.tools:search_repo"  # REQUIRED — dotted.path:function
capability = "repo.read"      # Which capability gates this tool
side_effects = "read"         # "none" | "read" | "write" (default: "none")
timeout_seconds = 60.0        # Per-call timeout (default: 60.0)
retries = 0                   # Retry count on failure (default: 0)
required_tier = "public"      # "public" | "internal" | "admin" (default: "public")

[tools.parameters]            # JSON Schema for parameters (optional)
type = "object"
required = ["query"]
[tools.parameters.properties.query]
type = "string"
description = "Search query"
```

**Note**: The `handler` field in TOML maps to `handler_ref` internally. Use `handler` in your manifests.

### Bootstrap Dependencies

```toml
[bootstrap]
deps = [
    { type = "pip", package = "requests", version = ">=2.0", optional = false },
    { type = "binary", package = "gh", optional = true },
    { type = "npm", package = "@scope/tool", optional = true },
    { type = "cargo", package = "my-cli", optional = true },
    { type = "brew", package = "jq", optional = true },
    { type = "pipx", package = "mycli", optional = true },
]
post_install = "echo done"         # Shell command after all deps installed (default: "")
check_command = "gh --version"     # Verify bootstrap succeeded (default: "")
```

| Type | Action | Target |
|------|--------|--------|
| `pip` | Install into `~/.obscura/venv/` via uv/pip | PyPI package |
| `uv` | Install via `uv pip install` | PyPI package |
| `npm` | `npm install -g` | npm package |
| `npx` | Verify npx available | npm package (on-demand) |
| `cargo` | `cargo install` | Rust crate |
| `binary` | Verify on PATH (no install) | System binary |
| `brew` | `brew install` | Homebrew formula |
| `pipx` | `pipx install` or `uv tool install` | Isolated CLI tool |

### Healthcheck

```toml
[healthcheck]
type = "binary"               # "callable" | "http" | "binary" | "python_import"
target = "gh"                 # Binary name, URL, or dotted import path
interval_seconds = 300        # Check interval (default: 300)
```

### Instructions

```toml
[[instructions]]
id = "review-guide"
scope = "agent"               # "global" | "agent" | "session" (default: "agent")
content = "Always check for security issues when reviewing code."
priority = 50                 # Lower = earlier in prompt (default: 50)
# version = "1.0.0"          # Defaults to "1.0.0" if omitted
```

### Policy Hints

```toml
[[policy_hints]]
capability_id = "repo.write"
recommended_action = "approve"  # "allow" | "deny" | "approve"
reason = "Write ops should require confirmation"
```

### Workflows

```toml
[[workflows]]
id = "review_pr"
name = "Review Pull Request"
description = "Automated PR review"
required_capabilities = ["repo.read", "pr.comment"]
steps = [
    { tool = "get_file", description = "Fetch changed files" },
    { tool = "comment_pr", description = "Post review" },
]
# version = "1.0.0"          # Defaults to "1.0.0" if omitted
```

---

## 6. Workspace Config

**Location**: `~/.obscura/config.toml` (global) or `.obscura/config.toml` (project-local)
**Purpose**: Runtime configuration. NOT a Kubernetes-enveloped spec.

```toml
mode = "code"                  # "code" | "ask" | "plan" | "diff" (default: "code")

[plugins]
load_builtins = true           # Load builtin plugins (default: true)

[plugins.bootstrap]
auto_install = true            # Auto-install plugin deps (default: true)
lenient_builtins = true        # Warn on builtin bootstrap failure, still register tools (default: true)

[defaults.capabilities]
grant = [                      # Capabilities granted by default
    "shell.exec",
    "file.read",
    "file.write",
    "git.ops",
    "web.browse",
    "search.web",
    "security.scan",
]
deny = []                      # Capabilities denied by default

[mcp]
auto_discover = true           # Auto-discover MCP servers (default: true)
```

Local `.obscura/config.toml` is deep-merged over global `~/.obscura/config.toml` (local wins on conflict).

---

## 7. Policy Rules

**Location**: `~/.obscura/policies/*.toml` or `.obscura/policies/*.toml`
**Purpose**: Fine-grained access rules for plugins, capabilities, and tools.
**Note**: NOT a Kubernetes-enveloped spec. Supports both list (`[[rules]]`) and dict (`[rules.<id>]`) syntax.

```toml
# List syntax
[[rules]]
id = "deny-shell-in-prod"     # Rule identifier (optional)
plugin = "my-plugin"          # Glob pattern for plugin ID. null = any (default: null)
trust_level = "untrusted"     # "builtin" | "verified" | "community" | "untrusted" (default: null)
capability = "shell.*"        # Glob pattern for capability ID (default: null)
tool = "run_shell"            # Glob pattern for tool name (default: null)
agent = "dev-*"               # Glob pattern for agent ID (default: null)
environment = "prod"          # "dev" | "staging" | "prod". null = any (default: null)
action = "deny"               # REQUIRED — "allow" | "deny" | "approve"
reason = "No shell in prod"   # Explanation (default: "")
priority = 100                # Higher = evaluated first (default: 0)
```

```toml
# Dict syntax (alternative — key becomes rule ID)
[rules.deny-shell-in-prod]
tool = "run_shell"
environment = "prod"
action = "deny"
reason = "No shell in production"
priority = 100
```

Glob patterns support `*` wildcards. Environment is matched against `OBSCURA_ENV` env var.

### Built-in Defaults (lowest priority)

| Trust Level | Action | Priority |
|-------------|--------|----------|
| builtin | allow | -100 |
| verified | allow | -100 |
| community | allow | -200 |
| untrusted | deny | -300 |

---

## 8. MCP Server Config

**Location**: `~/.obscura/mcp/core.json` or `.obscura/mcp/mcp.json`
**Format**: JSON (not TOML)

```json
{
  "mcpServers": {
    "server-name": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"],
      "env": { "KEY": "value" },
      "description": "Server description",
      "transport": "stdio"
    }
  }
}
```

Environment variables support `${VAR_NAME}` interpolation.

---

## 9. Directory Structure

```
~/.obscura/                        # Global home ($OBSCURA_HOME)
├── config.toml                    # Runtime config
├── agents.yaml                    # Agent pool (legacy YAML)
├── specs/
│   ├── templates/*.toml           # Agent templates
│   ├── policies/*.toml            # Policy specs
│   ├── workspaces/*.toml          # Workspace specs
│   └── packs/*.toml               # Pack specs
├── policies/*.toml                # Policy rule files
├── plugins/                       # User plugins
│   ├── <id>.toml                  # Flat layout
│   └── <id>/plugin.toml           # Subdirectory layout
├── mcp/core.json                  # MCP server config
├── venv/                          # Plugin dependency venv
├── events.db                      # SQLite event store
├── memory/                        # Key-value store
└── sessions/                      # Agent sessions

.obscura/                          # Project-local (overrides global)
├── config.toml                    # Merged over global
├── specs/                         # Same structure as global
├── policies/                      # Same structure as global
├── plugins/                       # Project-scoped plugins
└── docs/                          # Generated documentation
```

### Discovery Order

1. Global `~/.obscura/` loaded first
2. Local `.obscura/` overrides global
3. Specs deduplicated by `metadata.name`
4. Config deep-merged (local wins)

---

## 10. Compilation Pipeline

```
TOML/YAML spec files
        │
  [1. Loader]       Discover files, parse into Pydantic models
        │
  [2. Resolver]     Follow template extends, resolve policy/pack refs
        │
  [3. Merger]       Union lists, deep merge dicts, apply overrides
        │
  [4. Validator]    Check consistency, enforce constraints
        │
  [5. Compiled]     Frozen dataclasses (immutable, thread-safe)
```

### Override Precedence (lowest → highest)

1. Base template defaults
2. Child template overrides (`extends`)
3. Pack config/capabilities
4. Workspace config
5. Agent instance overrides
6. CLI flags

---

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `OBSCURA_HOME` | Override `~/.obscura` location |
| `OBSCURA_ENV` | Environment for policy matching (`dev`/`staging`/`prod`) |
| Plugin config keys | Read from env (e.g., `GITHUB_TOKEN`) |
"""


SPEC_GUIDE = """\
# Obscura Spec Guide

Specs are declarative TOML files that define agents, templates, policies, and packs.
All specs follow a Kubernetes-like envelope pattern.

---

## Envelope Format

Every spec file wraps its content in a standard envelope:

```toml
apiVersion = "obscura/v1"
kind = "Template"           # "Template" | "Workspace" | "Policy" | "Pack"

[metadata]
name = "my-spec"            # REQUIRED — unique identifier within kind
description = ""            # optional
tags = []                   # optional

[spec]
# kind-specific fields below
```

---

## Spec Kinds

### Template

Reusable agent blueprint. Templates can inherit from one parent via `extends`.

**Location**: `~/.obscura/specs/templates/*.toml` or `.obscura/specs/templates/*.toml`

```toml
[spec]
extends = "base-agent"         # Parent template name (single-level, default: null)
agent_type = "loop"            # "loop" | "daemon" | "reactive" | "scheduled" (default: "loop")
max_iterations = 25            # Max agent iterations (default: 25)
provider = "copilot"           # "copilot" | "claude" | "openai" | "localllm" | "moonshot" (default: "copilot")
model_id = "claude-sonnet-4-5-20250929"  # Optional model override (default: null)
instructions = "System prompt" # System prompt text (default: "")
plugins = ["gitleaks"]         # Plugin IDs to load (default: [])
capabilities = ["shell.exec"]  # Capabilities to enable (default: [])
tool_allowlist = ["read_file"]  # ONLY these tools. null = all (default: null)
tool_denylist = ["rm_rf"]       # Tools denied (default: [])
config = { key = "value" }     # Arbitrary runtime config (default: {})
input_schema = {}              # JSON Schema for input vars (default: null)
```

#### MCP Servers in Templates

```toml
[[spec.mcp_servers]]
name = "filesystem"
transport = "stdio"            # "stdio" | "sse" | "http" (default: "stdio")
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/path"]
env = { KEY = "value" }
```

#### Template Inheritance

When a child template sets `extends = "parent"`:
- Lists are **unioned** (deduplicated)
- Dicts are **deep merged** (child wins on conflict)
- Scalars are **overridden** by child

### Workspace

Top-level runtime entrypoint. Bundles agents, policies, plugins, and memory.

**Location**: `~/.obscura/specs/workspaces/*.toml` or `.obscura/specs/workspaces/*.toml`

```toml
[spec]
packs = ["code-quality-pack"]  # Pack names to include (default: [])
policies = ["safe-dev"]        # Policy names to apply (default: [])
config = { key = "value" }     # Runtime config overrides (default: {})

[spec.plugins]
include = ["docker-engine"]    # Plugin allowlist (default: [] = all)
exclude = ["untrusted"]        # Plugin denylist (default: [])

[spec.memory]
namespace = "my-ns"            # REQUIRED if memory block present
shared_scope = "workspace"     # "workspace" | "agent" | "session" (default: "workspace")
stores = ["key-value"]         # Store backends (default: [])
retention_days = 30            # default: 30

[[spec.agents]]
name = "dev"                   # REQUIRED
template = "base-agent"        # REQUIRED — template spec name
mode = "task"                  # "task" | "daemon" | "reactive" | "scheduled" (default: "task")
input = { repo_path = "." }    # Input variables (default: {})
overrides = {}                 # Template field overrides (default: {})

[spec.startup]
preload_plugins = true         # default: true
start_agents = ["dev"]         # Agent names to auto-start (default: [])
```

### Pack

Curated bundle of plugins, templates, policies, and config.

**Location**: `~/.obscura/specs/packs/*.toml` or `.obscura/specs/packs/*.toml`

```toml
[spec]
plugins = ["system-tools"]     # Plugin IDs (default: [])
templates = ["code-agent"]     # Recommended templates (default: [])
policies = ["safe-dev"]        # Policies to apply (default: [])
instructions = "Extra prompt"  # Prompt overlay (default: "")
config = { key = "value" }     # Config defaults (default: {})

[spec.capabilities]
grant = ["shell.exec"]         # Capabilities to enable (default: [])
deny = ["finance.quotes"]      # Capabilities to disable (default: [])
```

---

## Discovery Order

1. Global `~/.obscura/specs/` loaded first
2. Local `.obscura/specs/` overrides global
3. Specs deduplicated by `metadata.name` (local wins)

## Compilation Pipeline

```
TOML spec files
      |
[1. Loader]     Discover files, parse into Pydantic models
      |
[2. Resolver]   Follow template extends, resolve pack/policy refs
      |
[3. Merger]     Union lists, deep merge dicts, apply overrides
      |
[4. Validator]  Check consistency, enforce constraints
      |
[5. Compiled]   Frozen dataclasses (immutable, thread-safe)
```

### Override Precedence (lowest to highest)

1. Base template defaults
2. Child template overrides (`extends`)
3. Pack config/capabilities
4. Workspace config
5. Agent instance overrides
6. CLI flags
"""


POLICY_GUIDE = """\
# Obscura Policy Guide

Policies control what agents can do — which tools, plugins, and capabilities are
allowed, denied, or require user approval.

---

## Two Policy Systems

Obscura has two complementary policy systems:

### 1. Policy Specs (Compiler Pipeline)

Declarative constraints applied to a workspace via the spec envelope.

**Location**: `~/.obscura/specs/policies/*.toml`

```toml
apiVersion = "obscura/v1"
kind = "Policy"

[metadata]
name = "safe-dev"
description = "Conservative development policy"

[spec]
tool_allowlist = ["read_file"]       # ONLY these tools. null = all (default: null)
tool_denylist = ["rm_rf"]            # Tools denied (default: [])
require_confirmation = ["bash"]      # Tools needing user approval (default: [])
plugin_allowlist = ["system-tools"]  # ONLY these plugins. null = all (default: null)
plugin_denylist = []                 # Plugins denied (default: [])
max_turns = 25                       # Max agent iterations (default: 25)
token_budget = 0                     # Token limit, 0 = unlimited (default: 0)
base_dir = "/home/user/project"     # Filesystem root restriction (default: null)
allow_dynamic_tools = false          # Runtime tool registration (default: false)
```

Policies are referenced by name in workspace specs:

```toml
# In a workspace spec
[spec]
policies = ["safe-dev", "no-shell"]
```

### 2. Policy Rules (Plugin Policy Engine)

Fine-grained allow/deny/approve rules with glob-pattern matching.

**Location**: `~/.obscura/policies/*.toml` or `.obscura/policies/*.toml`
**Note**: These are NOT spec-enveloped files. They use `[[rules]]` directly.

```toml
[[rules]]
id = "deny-shell-in-prod"        # Rule identifier (optional, auto-generated if missing)
plugin = "my-plugin"             # Glob pattern for plugin ID (default: null = any)
trust_level = "untrusted"        # "builtin" | "verified" | "community" | "untrusted" (default: null)
capability = "shell.*"           # Glob pattern for capability ID (default: null)
tool = "run_shell"               # Glob pattern for tool name (default: null)
agent = "dev-*"                  # Glob pattern for agent ID (default: null)
environment = "prod"             # "dev" | "staging" | "prod" (default: null = any)
action = "deny"                  # REQUIRED — "allow" | "deny" | "approve"
reason = "No shell in prod"      # Explanation (default: "")
priority = 100                   # Higher = evaluated first (default: 0)
```

Rules support both list syntax (`[[rules]]`) and dict syntax (`[rules.<id>]`):

```toml
# Dict syntax (alternative)
[rules.deny-shell-in-prod]
tool = "run_shell"
environment = "prod"
action = "deny"
reason = "No shell in production"
priority = 100
```

---

## Policy Actions

| Action | Effect |
|--------|--------|
| `allow` | Tool/plugin is permitted |
| `deny` | Tool/plugin is blocked |
| `approve` | Permitted but requires user confirmation each call |

## Rule Matching

A rule matches when **all** specified matchers match. Unset matchers (null) match anything.

### Glob Patterns

Rules use simple glob matching with `*` wildcards:

- `"*"` — matches everything
- `"shell.*"` — matches `shell.exec`, `shell.read`, etc.
- `"obscura-*"` — matches `obscura-github`, `obscura-docker`, etc.

### Environment Matching

The `environment` field is matched against the `OBSCURA_ENV` environment variable.
Rules with no `environment` set apply to all environments.

### Priority

Rules are evaluated in **descending priority** order. First matching rule wins.

---

## Built-in Default Rules

These ship with Obscura at the lowest priority:

| Trust Level | Action | Priority |
|-------------|--------|----------|
| builtin | allow | -100 |
| verified | allow | -100 |
| community | allow | -200 |
| untrusted | deny | -300 |

User-defined rules (priority >= 0) always override these defaults.

---

## Evaluation API

```python
from obscura.plugins.policy import PluginPolicyEngine

engine = PluginPolicyEngine.load()

# Check if a plugin can be loaded
decision = engine.can_load_plugin("coingecko", trust_level="community")
print(decision.allowed)            # True
print(decision.requires_approval)  # False

# Check if a tool can be executed
decision = engine.can_execute_tool("run_shell", agent_id="dev")
print(decision.action)             # PolicyAction.ALLOW

# Check capability grants
decision = engine.can_grant_capability("shell.exec", agent_id="reviewer")
```
"""


CAPABILITY_GUIDE = """\
# Obscura Capability Guide

Capabilities are named permission surfaces that group tools together.
They provide a middle layer between plugins and individual tools.

---

## What Are Capabilities?

A capability is a logical permission that gates access to one or more tools.
Instead of granting tool-by-tool access, you grant capabilities.

```
Plugin: github
  Capability: repo.read  →  tools: [search_repo, get_file, list_branches]
  Capability: pr.write   →  tools: [comment_pr, approve_pr, merge_pr]
```

---

## Defining Capabilities (Plugin Manifests)

Capabilities are declared in plugin manifest TOML files.

### List Syntax

```toml
[[capabilities]]
id = "repo.read"
description = "Read repository contents"
tools = ["search_repo", "get_file"]
requires_approval = false
default_grant = true
version = "1.0.0"

[[capabilities]]
id = "pr.write"
description = "Write to pull requests"
tools = ["comment_pr", "approve_pr"]
requires_approval = true
default_grant = false
```

### Dict Syntax (Alternative)

```toml
[capabilities.repo_read]
id = "repo.read"
description = "Read repository contents"
tools = ["search_repo", "get_file"]

[capabilities.pr_write]
id = "pr.write"
description = "Write to pull requests"
tools = ["comment_pr"]
requires_approval = true
default_grant = false
```

---

## Capability Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | string | REQUIRED | Dot-separated identifier (e.g., `repo.read`) |
| `description` | string | `""` | What this capability grants |
| `tools` | list[string] | `[]` | Tool names gated by this capability |
| `requires_approval` | bool | `false` | User must approve each call |
| `default_grant` | bool | `true` | Granted by default when plugin loads |
| `version` | string | `"1.0.0"` | Capability version |

---

## Granting Capabilities

### In Template Specs

```toml
# ~/.obscura/specs/templates/code-agent.toml
[spec]
capabilities = ["shell.exec", "file.read", "git.ops", "repo.read"]
```

### In Workspace Config

```toml
# ~/.obscura/config.toml
[defaults.capabilities]
grant = ["shell.exec", "file.read", "file.write", "git.ops"]
deny = ["finance.quotes"]
```

### In Pack Specs

```toml
# ~/.obscura/specs/packs/code-quality-pack.toml
[spec.capabilities]
grant = ["shell.exec", "security.scan"]
deny = ["finance.quotes"]
```

---

## Capability Policy Rules

Policy rules can target capabilities by ID using glob patterns:

```toml
# ~/.obscura/policies/strict.toml
[[rules]]
id = "approve-all-writes"
capability = "*.write"
action = "approve"
reason = "All write capabilities require user confirmation"
priority = 50

[[rules]]
id = "deny-finance"
capability = "finance.*"
action = "deny"
reason = "Financial capabilities disabled"
priority = 100
```

---

## How Capabilities Flow

```
1. Plugin declares capabilities + tools in manifest
2. Template spec lists which capabilities to enable
3. Workspace config may override grants/denials
4. Pack specs add/remove capabilities
5. Policy rules evaluate at runtime per-call
6. ToolBroker checks capability before executing any tool
```

---

## Naming Conventions

Use dot-separated hierarchical IDs:

| Pattern | Example |
|---------|---------|
| `domain.action` | `shell.exec`, `file.read`, `git.ops` |
| `service.scope` | `github.repos`, `docker.containers` |
| `category.level` | `security.scan`, `web.browse` |

---

## Policy Hints

Plugins can recommend access rules for their capabilities:

```toml
[[policy_hints]]
capability_id = "pr.write"
recommended_action = "approve"
reason = "Write operations should require user confirmation"
```

Policy hints are advisory — the actual policy engine rules take precedence.
Hints are useful for plugin authors to communicate their intent.
"""
