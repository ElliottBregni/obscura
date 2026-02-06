# Agent-Specific File Routing

This vault supports **dual-level agent routing** to manage context for multiple AI systems (Copilot, Claude, Cursor, etc.).

## Routing Methods

The system supports **three routing methods** with clear priority rules.

### 1. Directory-Level Routing (Highest Priority)
Create agent-specific directories with `.agent` suffix:

```
vault/
  skills/              # Universal (all agents see)
    general.md
  skills.copilot/      # Copilot-specific override
    python.md          # Overrides skills/python.md for Copilot
  skills.claude/       # Claude-specific override
    python.md          # Overrides skills/python.md for Claude
```

**Rules:**
- Files in `DIR.agent/` override files in `DIR/` by relative path
- Merged overlay: `skills.copilot/python.md` → `skills/python.md` (in Copilot's view)
- Works for ANY directory: `src/`, `config/`, `tests/`, etc.
- **Highest priority**: Wins over nested and universal files

### 2. Nested File Overrides (Medium Priority)
Agent-specific files **within** universal directories override universal files by basename:

```
vault/skills/
  setup.md              # Universal
  setup.copilot.md      # Copilot override (same directory)
  setup.claude.md       # Claude override (same directory)
```

**Rules:**
- Pattern: `filename.agent.ext` within any directory
- Overrides universal file with same base name in **same directory**
- Result stays in same path: `skills/setup.copilot.md` → `skills/setup.md` (agent content)
- Cleaner vault structure - related files grouped together
- **Medium priority**: Wins over universal, but loses to directory-level overrides

### 3. Root-Level File Routing (Agent-Specific Paths)
Use `.agent.` in filename **at vault root** to route to agent-specific target paths:

```
vault/
  skills.copilot.md    → .github/skills.md (in repo)
  config.claude.yaml   → .claude/config.yaml (in repo)
  prompt.copilot.txt   → .github/prompt.txt (in repo)
```

**Rules:**
- Pattern: `filename.agent.ext` at vault root (maxdepth 1)
- Copilot files (`*.copilot.*`) → `.github/` in target repo
- Claude files (`*.claude.*`) → `.claude/` in target repo
- Agent suffix stripped in destination: `skills.copilot.md` becomes `skills.md`
- **Note**: Files in subdirectories use nested override behavior, not routing

## Priority Rules

When multiple agent mechanisms exist for the same file:

1. **AGENT** (Directory-level): `skills.copilot/setup.md` - **WINS ALWAYS**
2. **AGENT_NESTED** (In-directory override): `skills/setup.copilot.md` - **WINS over UNIVERSAL**
3. **UNIVERSAL**: `skills/setup.md` - **Lowest priority**
4. **AGENT_FILE** (Root routing): `setup.copilot.md` at root → `.github/setup.md` (separate path)

Example with all three:
```
vault/
  skills/setup.md              # Priority 3: Universal
  skills/setup.copilot.md      # Priority 2: Nested override
  skills.copilot/setup.md      # Priority 1: Directory override (WINS)
```

**Result for Copilot**: `skills/setup.md` = content from `skills.copilot/setup.md` (AGENT wins)

## Agent-to-Path Mapping

| Agent    | File Pattern      | Target Path      |
|----------|-------------------|------------------|
| copilot  | `*.copilot.*`     | `.github/`       |
| claude   | `*.claude.*`      | `.claude/`       |
| cursor   | `*.cursor.*`      | `.cursor/`       |
| custom   | `*.custom.*`      | `.custom/`       |

## Use Cases

### Directory-Level: Complete Override
Best when you want an entirely different directory structure for an agent:
- `skills.copilot/` - Completely replace all skills for Copilot
- `config.claude/` - Different config structure for Claude
- Wholesale replacement of directory contents

### Nested Overrides: Granular Control
Best for selective overrides while keeping structure:
- `skills/setup.copilot.md` - Override one file, keep others universal
- `config/api.copilot.yaml` - Agent-specific config, other configs shared
- Clean vault: related files grouped together
- Easy to see what's overridden (agent files next to universal)

### Root-Level Routing: Isolated Agent Files
Best for content that only exists for one agent and doesn't belong in shared structure:
- `quickstart.copilot.md` at root → `.github/quickstart.md`
- `prompts.claude.txt` at root → `.claude/prompts.txt`
- Temporary or experimental agent-specific content

## Examples

### Example 1: Nested overrides for clean vault
```
vault/skills/
  git-workflow.md         # Universal (all agents)
  python.md               # Universal default
  python.copilot.md       # Copilot sees this instead
  python.claude.md        # Claude sees this instead
  testing.md              # Universal (no overrides)
```

Result:
- Copilot: git-workflow.md (universal), python.md (from python.copilot.md), testing.md (universal)
- Claude: git-workflow.md (universal), python.md (from python.claude.md), testing.md (universal)

### Example 2: Directory override (highest priority)
```
vault/
  config/api.yaml              # Universal
  config/api.copilot.yaml      # Nested override
  config.claude/api.yaml       # Directory override for Claude
```

Result:
- Copilot: `config/api.yaml` from `config/api.copilot.yaml` (nested override)
- Claude: `config/api.yaml` from `config.claude/api.yaml` (directory wins over nested)

### Example 3: Root-level routing to agent paths
```
vault/
  quickstart.copilot.md    # At root
  prompts.claude.txt       # At root
```

Result:
- Copilot repo: `.github/quickstart.md` (from root file)
- Claude repo: `.claude/prompts.txt` (from root file)

### Example 4: All three methods combined
```
vault/
  skills/
    git.md                    # Universal
    python.md                 # Universal
    python.copilot.md         # Nested override for Copilot
  skills.claude/              # Directory override for Claude
    python.md
    database.md
  api.copilot.md              # Root routing → .github/api.md
```

Copilot sees:
- `skills/git.md` (universal)
- `skills/python.md` (from nested `python.copilot.md`)
- `.github/api.md` (from root `api.copilot.md`)

Claude sees:
- `skills/git.md` (universal, no override)
- `skills/python.md` (from `skills.claude/python.md` - directory wins)
- `skills/database.md` (from `skills.claude/database.md` - claude-only)

## Combining Both Methods

You can use directory-level AND file-level routing together:

```
vault/
  skills/              # Universal skills
    git.md
  skills.copilot/      # Copilot directory override
    python.md
  api.copilot.md       # Copilot file-level routing → .github/api.md
  config.claude.yaml   # Claude file-level routing → .claude/config.yaml
```

Copilot sees:
- `skills/git.md` (universal)
- `skills/python.md` (from `skills.copilot/`)
- `.github/api.md` (from `api.copilot.md`)

Claude sees:
- `skills/git.md` (universal)
- `skills/python.md` (universal, no override)
- `.claude/config.yaml` (from `config.claude.yaml`)

## Implementation Details

- Merge logic in `watch-and-sync.sh:merge_agent_overlay()`
- File-level detection: `find -name "*.${agent}.*"`
- Agent suffix stripped: `sed "s/\.${agent}\././"`
- Applies to watch mode and symlink mode (future)
- Both methods processed in single merge pass

## When to Use Which

| Scenario | Use Directory-Level | Use File-Level |
|----------|---------------------|----------------|
| Agent-specific version of existing file | ✅ Yes | ❌ No |
| Agent-only content (no universal) | ❌ Awkward | ✅ Yes |
| Many related files | ✅ Yes (group in dir) | ❌ Clutters vault root |
| Single file | ❌ Overkill | ✅ Yes |
| Existing hierarchy to preserve | ✅ Yes | ❌ Must create nested files |
| Quick experiment | ❌ Requires dir setup | ✅ Yes (drop file in) |

## See Also

- [MULTI-AGENT.md](./MULTI-AGENT.md) - Full multi-agent architecture
- [AUTO-SYNC.md](./AUTO-SYNC.md) - Watch mode and syncing
- `agents/INDEX.md` - Agent registry

## Recommended Patterns

### Pattern 1: Start Universal, Add Overrides
```
1. Create universal file: skills/python.md
2. Test with both agents
3. If agent needs customization: add skills/python.copilot.md
4. Universal file stays as fallback for other agents
```

### Pattern 2: Agent-Specific Experiments
```
1. Create nested override: config/api.copilot.yaml
2. Test with Copilot
3. If successful, promote to universal: mv to config/api.yaml
4. Or keep as override if truly agent-specific
```

### Pattern 3: Directory Replacement
```
1. Realize entire directory needs agent-specific structure
2. Create skills.claude/ directory
3. Copy and modify all files
4. Directory override ensures complete isolation
```

## Migration Guide

### From Flat Structure
**Before:**
```
vault/
  skills.copilot.md         # Root-level routing
  config.claude.yaml        # Root-level routing
```

**After (nested structure):**
```
vault/skills/
  setup.copilot.md          # Nested override
vault/config/
  api.claude.yaml           # Nested override
```

**Benefit:** Related files grouped, cleaner vault root

### From Directory Overrides
**Before:**
```
vault/
  skills.copilot/           # Full directory
    python.md
    git.md
    api.md
```

**After (selective nested):**
```
vault/skills/
  python.md                 # Universal
  python.copilot.md         # Override only what's needed
  git.md                    # Universal
  api.md                    # Universal
```

**Benefit:** Less duplication, clear what's customized

## Troubleshooting

### Override Not Working
**Symptom:** Universal file still appears in agent overlay

**Check:**
1. Filename pattern: Must include `.agent.` (e.g., `setup.copilot.md`)
2. Extension after agent: `setup.copilot` won't work, needs `setup.copilot.md`
3. Agent registered: Check `agents/INDEX.md`
4. Basename matches: `setup.copilot.md` overrides `setup.md`, not `setup-guide.md`

### Wrong Priority
**Symptom:** Nested override not winning, or directory not winning

**Priority check:**
- Directory override (AGENT) beats everything for that path
- Nested override (AGENT_NESTED) beats UNIVERSAL only
- If directory override exists, nested override ignored

**Solution:** Remove directory override if you want nested override to work

### File Not Found
**Symptom:** Agent-specific file not appearing in overlay

**Check:**
1. File in correct location: vault root for routing, subdirs for nested
2. Pattern correct: `*.agent.*` with agent name matching `agents/INDEX.md`
3. Run with correct agent flag: `--agent copilot`

## Implementation

- Merge logic: `watch-and-sync.sh:merge_agent_overlay()`
- Directory scan: `find -maxdepth 1 -type d -name "*.${agent}"`
- Nested detection: `[[ "$filename" =~ \.${agent}\. ]]`
- Root routing: `find -maxdepth 1 -type f -name "*.${agent}.*"`
- Priority merge: AWK associative arrays with type-based override logic

## See Also

- [agents/INDEX.md](../agents/INDEX.md) - Agent registry
- [MULTI-AGENT.md](./MULTI-AGENT.md) - Architecture overview
- [AUTO-SYNC.md](./AUTO-SYNC.md) - Watch mode and syncing
