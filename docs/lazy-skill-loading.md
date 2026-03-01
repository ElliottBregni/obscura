# Lazy Skill Loading for Obscura

## Overview

Lazy skill loading reduces context window bloat by loading only skill metadata (name + description) initially, and loading full skill bodies on-demand when they're invoked.

## Impact

- **Before**: ~3,755 tokens (all 7 skills loaded upfront)
- **After**: ~693 tokens (metadata only)
- **Savings**: 81.5% reduction (3,062 tokens)

## Usage

### Enable Lazy Loading

```python
from obscura.core.context import ContextLoader
from obscura.core.types import Backend

# Create loader with lazy loading enabled
loader = ContextLoader(
    Backend.CLAUDE,
    lazy_load_skills=True,  # Enable lazy loading
    skill_filter=["pytight", "authority"]  # Optional: limit available skills
)

# System prompt now contains only skill stubs
system_prompt = loader.load_system_prompt()

# Load specific skill on-demand
skill_body = loader.load_skill_body("pytight")
```

### Agent Configuration

Add skill configuration to `~/.obscura/agents.yaml`:

```yaml
agents:
  - name: assistant
    type: loop
    model: copilot
    system_prompt: "You are a helpful assistant."
    max_turns: 25
    mcp_servers: auto
    skills:
      lazy_load: true  # Enable lazy loading
      filter:  # Optional: only make these skills available
        - pytight  # Python code quality
        # Other skills available on-demand when user requests them
```

## Implementation

### New Classes

- **`LazySkillLoader`** (`obscura/core/context_lazy.py`): Manages skill discovery and on-demand loading
- **`SkillMetadata`**: Lightweight skill metadata container

### Modified Classes

- **`ContextLoader`** (`obscura/core/context.py`): Extended with lazy loading support
  - `lazy_load_skills` parameter: Enable/disable lazy loading
  - `skill_filter` parameter: Limit available skills
  - `load_skills_lazy()`: Load metadata only
  - `load_skill_body(name)`: Load full skill on-demand

## Workflow

1. **Agent starts**: Only skill metadata loaded (~100 tokens per skill)
2. **User requests skill**: Full skill body loaded (~450 tokens)
3. **Skill cached**: Subsequent requests use cached version
4. **Session ends**: Cache cleared

## Benefits

1. **Reduced initial context**: 81.5% fewer tokens
2. **Faster startup**: Less data to load and parse
3. **Agent-specific skills**: Each agent only loads what it needs
4. **Pay-per-use**: Only load skills when actually used
5. **Backward compatible**: Existing code works with `lazy_load_skills=False`

## Testing

```bash
cd ~/dev/obscura-main
python test_lazy_skills.py
```

Expected output:
```
Eager loading:  3,755 tokens
Lazy loading:   693 tokens
Savings:        81.5%
Reduction:      3,062 tokens
```

## Next Steps

1. Integrate with agent runtime to enable lazy loading by default
2. Add configuration support in `agents.yaml`
3. Create skill invocation tool that triggers on-demand loading
4. Monitor cache hit rates and optimize
