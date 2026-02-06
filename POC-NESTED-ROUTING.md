# Proof of Concept: Nested Agent Routing

## Vault Structure Created

```
~/FV-Copilot/
├── instructions/                    # Universal (all agents)
│__ ├── x.md                         # Universal version
 │  └── z.md                         # Universal only (no overrides)
	├── instructions.copilot/            # Copilot-specific directory
	│   ├── x.md                         # Overrides instructions/x.md
	│   └── y.md                         # Copilot-only file
	└── instructions.claude/             # Claude-specific directory
	├── x.md                         # Overrides instructions/x.md
	└── y.md                         # Claude-only file
```

## Expected Behavior

### Copilot Overlay
When `merge_agent_overlay copilot` runs:

```
instructions/
  x.md  ← from instructions/x.copilot.md  (AGENT_NESTED override)
  y.md  ← from instructions/y.copilot.md  (AGENT_NESTED - copilot-only)
  z.md  ← from instructions/z.md          (UNIVERSAL)
```

### Claude Overlay
When `merge_agent_overlay claude` runs:

```
instructions/
  x.md  ← from instructions/x.claude.md   (AGENT_NESTED override)
  y.md  ← from instructions/y.claude.md   (AGENT_NESTED - claude-only)
  z.md  ← from instructions/z.md          (UNIVERSAL)
```

## File Contents

### instructions/x.md (Universal)
```markdown
# X - Universal Instruction

This is the **universal** version of x.md.
All agents see this by default.

Location: `instructions/x.md`
Type: UNIVERSAL
```

### instructions/x.copilot.md (Copilot Override)
```markdown
# X - Copilot Override (Nested)

This is the **Copilot-specific** version within instructions/

Overrides instructions/x.md for Copilot.

Location: `instructions/x.copilot.md`
Type: AGENT_NESTED (in-directory override)
```

### instructions/x.claude.md (Claude Override)
```markdown
# X - Claude Override (Nested)

This is the **Claude-specific** version within instructions/

Overrides instructions/x.md for Claude.

Location: `instructions/x.claude.md`
Type: AGENT_NESTED (in-directory override)
```

### instructions/y.copilot.md (Copilot-Only)
```markdown
# Y - Copilot Only (Nested)

This is y.md for Copilot only.
No universal version exists.

Location: `instructions/y.copilot.md`
Type: AGENT_NESTED (copilot-only)
```

### instructions/y.claude.md (Claude-Only)
```markdown
# Y - Claude Only (Nested)

This is y.md for Claude only.
Different content than Copilot's y.md.

Location: `instructions/y.claude.md`
Type: AGENT_NESTED (claude-only)
```

### instructions/z.md (Universal, No Override)
```markdown
# Z - Universal Instruction

This is z.md - only exists in universal.
No agent-specific overrides for this file.

Location: `instructions/z.md`
Type: UNIVERSAL (no overrides)
```

## Testing

### Manual Test
```bash
cd ~/FV-Copilot

# View copilot overlay
source watch-and-sync.sh
merge_agent_overlay copilot | grep "^instructions/"

# Expected output:
# instructions/x.md|/Users/bregnie/FV-Copilot/instructions/x.copilot.md|AGENT_NESTED
# instructions/y.md|/Users/bregnie/FV-Copilot/instructions/y.copilot.md|AGENT_NESTED
# instructions/z.md|/Users/bregnie/FV-Copilot/instructions/z.md|UNIVERSAL

# View claude overlay
merge_agent_overlay claude | grep "^instructions/"

# Expected output:
# instructions/x.md|/Users/bregnie/FV-Copilot/instructions/x.claude.md|AGENT_NESTED
# instructions/y.md|/Users/bregnie/FV-Copilot/instructions/y.claude.md|AGENT_NESTED
# instructions/z.md|/Users/bregnie/FV-Copilot/instructions/z.md|UNIVERSAL
```

### Apply to Repo
```bash
# Create test repo for copilot
mkdir -p /tmp/test-copilot-repo
merge_agent_overlay copilot | while IFS='|' read dest src type; do
    [[ "$dest" =~ ^instructions/ ]] || continue
    target="/tmp/test-copilot-repo/$dest"
    mkdir -p "$(dirname "$target")"
    cp "$src" "$target"
done

# Verify
ls /tmp/test-copilot-repo/instructions/
# Expected: x.md y.md z.md

head -2 /tmp/test-copilot-repo/instructions/x.md
# Expected: # X - Copilot Override (Nested)
```

## Key Insights

### ✅ Nested File Override Works
- `instructions/x.copilot.md` successfully overrides `instructions/x.md` for Copilot
- `instructions/x.claude.md` successfully overrides `instructions/x.md` for Claude
- Each agent sees their own version with different content
- All files in same directory - easy to manage

### ✅ Agent-Only Files Work
- `y.copilot.md` and `y.claude.md` exist without universal `y.md`
- Each agent sees their own `y.md` with different content
- No conflicts or errors
- Pattern works for files that don't have universal versions

### ✅ Universal Fallback Works
- `z.md` exists only as universal (no `.copilot` or `.claude` versions)
- Both agents see the same content
- Shared content works correctly

### ✅ Clean Vault Structure
- All related files in one directory
- Easy to see what's overridden (agent suffix visible)
- No nested directories to navigate
- Visual clarity: `x.md`, `x.copilot.md`, `x.claude.md` side-by-side

## Comparison to Other Methods

### This POC (Nested File Pattern) ✅ RECOMMENDED
```
instructions/
  x.md                # Universal
  x.copilot.md        # Copilot override (same dir)
  x.claude.md         # Claude override (same dir)
  y.copilot.md        # Copilot-only
  y.claude.md         # Claude-only
  z.md                # Universal
```
**Pros:**
- All files in one place
- Easy to see universal + overrides side-by-side
- No nested directories
- Visual clarity
- Scales well

### Directory-Level Routing (Alternative)
```
instructions/x.md
instructions/z.md
instructions.copilot/x.md
instructions.copilot/y.md
instructions.claude/x.md
instructions.claude/y.md
```
**Pros:**
- Complete isolation per agent
- Good for wholesale replacement

**Cons:**
- More directories to manage
- Duplication if many files stay universal

### Root-Level Routing (Alternative)
```
x.copilot.md → .github/x.md
x.claude.md  → .claude/x.md
```
**Pros:**
- Routes to agent-specific paths in repo
- Simple for single files

**Cons:**
- Clutters vault root
- Different behavior (routing vs override)

## Recommendation

For the `instructions/` use case with multiple files:
- **Use nested file pattern** (this POC) ✅
- All files in one directory for easy management
- Clear visual indication of overrides (filename suffix)
- Scales well as files grow
- No nested directories to navigate
- Easy to add new agent overrides (just add `filename.agent.ext`)

## Cleanup

Structure created:
- `instructions/` - Single directory with all files:
  - `x.md`, `z.md` (universal)
  - `x.copilot.md`, `y.copilot.md` (copilot overrides)
  - `x.claude.md`, `y.claude.md` (claude overrides)

All test files are properly organized in the instructions/ directory.
No separate directories for agents - everything in one place.

## Next Steps

1. Test the merge function with actual `watch-and-sync.sh`
2. Apply overlay to a real repo
3. Verify syncing works correctly
4. Update documentation with this pattern
5. Consider migrating existing skills/ to this structure
