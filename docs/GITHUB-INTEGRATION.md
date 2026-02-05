# GitHub Integration

## Copilot Instructions Location

GitHub Copilot CLI looks for instructions in these locations (in order):
1. `.github/copilot-instructions.md` ← **Official location**
2. `.github/instructions/**/*.instructions.md`
3. `AGENTS.md` (in git root)
4. `$HOME/.copilot/copilot-instructions.md`

## Our Vault Structure

**Flattened design:**
```
~/FV-Copilot/repos/YourRepo/        # IS the .github content!
├── copilot-instructions.md          # Main instructions
├── instructions/                    # Additional context
├── skills/                          # Copilot skills
└── platform/module/                 # Nested modules
    ├── copilot-instructions.md      # Module-specific
    └── instructions/                # Module docs
```

**Repo symlinks:**
```
~/git/YourRepo/
├── .github → vault/repos/YourRepo/
└── platform/module/
    └── .github → vault/repos/YourRepo/platform/module/
```

## Managing .github in the Vault

### Sync Script

```bash
cd ~/git/YourRepo
~/FV-Copilot/sync-github.sh --dry-run  # Preview
~/FV-Copilot/sync-github.sh            # Apply
```

This automatically:
- ✅ Syncs root `.github`
- ✅ Finds nested modules with content
- ✅ Creates symlinks only where code exists
- ✅ Skips vault-only folders (skills/, instructions/)

### Nested Module Example

If vault has:
```
repos/YourRepo/platform/service/copilot-instructions.md
```

And repo has:
```
~/git/YourRepo/platform/service/  ← actual code directory
```

Script creates:
```
~/git/YourRepo/platform/service/.github → vault
```

**But if** vault has `skills/` with no matching repo folder:
- ❌ No symlink created
- ✅ Content stays vault-only for reference

## What Goes in copilot-instructions.md

### Template
```markdown
# Project/Module Name

## Overview
Brief description of what this code does

## Key Patterns
- Important conventions
- Tech stack specifics
- Common patterns

## Development
- How to run/test
- Important commands
- Gotchas to avoid

## Context for AI
- Coding standards
- Links to detailed docs
```

### Best Practices
- ✅ Keep it concise
- ✅ Focus on code-specific context
- ✅ Put detailed docs in `instructions/`
- ❌ Don't duplicate general knowledge
- ❌ Avoid overly long files

## Vault-Only vs Repo Folders

**Repo folders (symlinked):**
- Code exists in actual repo
- `.github` created automatically
- Team can see content (if committed)

**Vault-only folders:**
- No matching code directory
- Skills, reference docs, experiments
- Personal context, not in repo

## Workflow

1. **Create content in vault** (edit in Obsidian or any editor)
2. **Script creates symlinks** (only where code exists)
3. **Commit to repo** (optional, for team sharing)
4. **Iterate freely** (vault-only folders never touch repo)

## Example Full Structure

```
Vault (~/FV-Copilot/):
repos/
└── YourRepo/
    ├── copilot-instructions.md      ← Repo root context
    ├── skills/                      ← Vault-only (no repo match)
    └── platform/service/
        ├── copilot-instructions.md  ← Module context
        └── instructions/            ← Detailed docs
            └── *.md

Repo (~/git/YourRepo/):
├── .github → vault/repos/YourRepo/
└── platform/service/
    ├── .github → vault/repos/YourRepo/platform/service/
    └── actual_code.py
```

Clean, simple, powerful! 🎯
