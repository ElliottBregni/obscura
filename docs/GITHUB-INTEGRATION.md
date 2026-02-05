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
- ❌ No dedicated `.github` symlink at `repo/skills/.github`
- ✅ Still accessible via parent: `repo/.github/skills/`
- ✅ Content available to Copilot, just not as a separate module

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

**Repo folders (get dedicated .github):**
- Code directory exists in actual repo
- Script creates dedicated `.github` symlink at that location
- Team sees it as a module-specific context

**Vault-only folders (accessible through parent):**
- No matching code directory in repo
- NO dedicated `.github` symlink for that folder
- Still accessible via parent `.github` symlink
- Example: `repo/.github/skills/` works, but `repo/skills/.github` doesn't exist
- Use for: skills, reference docs, experiments

## Workflow

1. **Create content in vault** (edit in Obsidian or any editor)
2. **Script creates symlinks** (only where code directories exist)
3. **Vault-only folders automatically accessible** (via parent .github)
4. **Commit to repo** (optional, for team sharing)
5. **Iterate freely** (add vault folders anytime, no repo changes needed)

## Example Full Structure

```
Vault (~/FV-Copilot/):
repos/
└── YourRepo/
    ├── copilot-instructions.md      ← Repo root context
    ├── skills/                      ← Vault-only (accessible via root .github)
    └── platform/service/
        ├── copilot-instructions.md  ← Module context
        └── instructions/            ← Vault-only (accessible via module .github)
            └── *.md

Repo (~/git/YourRepo/):
├── .github → vault/repos/YourRepo/
│   ├── copilot-instructions.md      ✅ Visible
│   └── skills/                      ✅ Visible (vault-only, through parent)
└── platform/service/
    ├── .github → vault/repos/YourRepo/platform/service/
    │   ├── copilot-instructions.md  ✅ Visible
    │   └── instructions/            ✅ Visible (vault-only, through parent)
    └── actual_code.py
```

**Key insight:** Vault-only folders are visible through parent `.github`, they just don't get their own dedicated `.github` symlink!

Clean, simple, powerful! 🎯
