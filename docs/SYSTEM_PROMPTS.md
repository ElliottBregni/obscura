# System Prompts

Obscura automatically provides all agents with a comprehensive system prompt that explains:
- Available tools and capabilities
- Obscura architecture and codebase structure
- Memory system usage
- Security guardrails
- Best practices

## Default System Prompt

Every agent spawned in Obscura automatically receives a default system prompt that includes:

### 1. Runtime Information
- Platform details (macOS ARM64, etc.)
- Available tools (25+ system tools)
- Agent capabilities (web search, shell access, file I/O, etc.)

### 2. Architecture Context
- Obscura's 4-layer architecture
- Agent's role as Layer 2 (Agent Runtime)
- Access to memory, telemetry, and session management

### 3. Codebase Structure
- Package organization (`core/`, `providers/`, `tools/`, etc.)
- Module responsibilities
- Key subsystems (auth, memory, integrations)

### 4. Tool Reference
- Web access (`web_search`, `web_fetch`)
- Execution (`run_shell`, `run_python3`, `run_npx`)
- File operations (`read_text_file`, `write_text_file`, etc.)
- System inspection (`get_system_info`, `list_processes`, `list_ports`)
- Security tools (`security_lookup`, `list_unix_capabilities`)
- Coordination (`task` delegation, `manage_crontab`)

### 5. Memory System
- Key-value memory API
- Vector/semantic memory
- CLI and API access patterns

### 6. Security Guardrails
- Filesystem sandboxing (`base_dir`)
- Command deny lists (sudo, rm -rf, etc.)
- Execution timeouts (30s)
- Process signal restrictions

### 7. Best Practices
- Proactive tool usage
- Tool chaining
- Read-before-write patterns
- System state verification
- Appropriate delegation

## Usage

### Default Behavior

By default, all agents get the Obscura system prompt automatically:

```python
# Via API
POST /api/v1/agents
{
  "name": "my-agent",
  "model": "claude"
}
# Agent will have default Obscura prompt

# Via CLI
obscura agent spawn --name my-agent --model claude
# Agent will have default Obscura prompt
```

### Custom System Prompt

You can provide your own prompt, which will be appended to the default:

```python
POST /api/v1/agents
{
  "name": "my-agent",
  "model": "claude",
  "system_prompt": "You are an expert Python developer."
}
# Agent gets: [Obscura default] + [Your custom prompt]
```

### Disable Default Prompt

Set environment variable to disable the default:

```bash
export OBSCURA_INCLUDE_DEFAULT_PROMPT=false
obscura serve
```

Now agents will only receive custom prompts you provide:

```python
POST /api/v1/agents
{
  "name": "my-agent",
  "model": "claude",
  "system_prompt": "You are a helpful assistant."
}
# Agent gets only: "You are a helpful assistant."
```

### With Skills

Skills are appended to the prompt automatically:

```python
POST /api/v1/agents
{
  "name": "my-agent",
  "model": "claude",
  "system_prompt": "You are a code reviewer.",
  "builder": {
    "skills": [
      {
        "name": "Python Expert",
        "content": "Expert in Python best practices, PEP 8, type hints.",
        "source": "inline"
      }
    ]
  }
}
# Result: [Obscura default] + [Custom prompt] + [Skills section]
```

## Programmatic Access

### Get Default Prompt

```python
from obscura.core.system_prompts import get_default_system_prompt

prompt = get_default_system_prompt()
print(prompt)
```

### Compose Custom Prompt

```python
from obscura.core.system_prompts import compose_system_prompt

# With default + custom
composed = compose_system_prompt(
    base="You are a helpful assistant.",
    include_default=True,
    custom_sections=["## Additional Context", "..."]
)

# Custom only
custom_only = compose_system_prompt(
    base="You are an expert.",
    include_default=False
)
```

### Load from File

```python
from obscura.core.system_prompts import load_custom_system_prompt

prompt = load_custom_system_prompt("~/.obscura/my_prompt.md")
```

## Why Default Prompts Matter

**Problem:** Agents often don't know what capabilities they have. They say "I can't search the web" when they actually can, or try to use tools that don't exist.

**Solution:** The default system prompt explicitly tells agents:
- What tools are available
- How to use them
- When to use them proactively
- What the system architecture looks like

**Result:** Agents that:
- Use `web_search` when asked about trends/current events
- Read files with `read_text_file` when mentioned
- Inspect system state with `get_system_info`/`list_processes`
- Understand Obscura's codebase structure for development tasks

## Example Agent Behavior

### Without Default Prompt
```
User: "Search for Python trends"
Agent: "I can't search the web. Here's what I know about Python..."
```

### With Default Prompt
```
User: "Search for Python trends"
Agent: [calls web_search tool]
       "Based on web search results:
        1. Async/await becoming standard...
        2. Type hints with Pyright...
        [actual current trends from web]"
```

## Best Practices

### For Agent Developers

1. **Trust the default** - Don't duplicate tool documentation in custom prompts
2. **Add domain context** - Custom prompts should add domain-specific knowledge
3. **Use skills** - Package reusable expertise as skills, not inline prompts

### For System Operators

1. **Keep default enabled** - Disable only if you have a complete replacement
2. **Monitor agent behavior** - Check if agents use tools appropriately
3. **Update periodically** - As new tools are added, update the default prompt

## Configuration Reference

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `OBSCURA_INCLUDE_DEFAULT_PROMPT` | `true` | Include default Obscura prompt for all agents |

## Implementation Details

**Location:** `obscura/core/system_prompts.py`

**Integration point:** `obscura/routes/agents.py:_compose_system_prompt()`

**Composition order:**
1. Default Obscura prompt (if enabled)
2. User-provided system prompt (if any)
3. Skills section (if any)

**Separator:** `\n\n---\n\n` between sections

## Future Enhancements

Planned improvements:
- Per-agent prompt customization (override default per agent)
- Prompt templates (versioned defaults)
- Dynamic tool discovery (auto-generate tool list)
- Context-aware prompts (adjust based on available tools)
- Localization (prompts in multiple languages)
