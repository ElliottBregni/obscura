# GitHub Integration

## Copilot Instructions Location

GitHub Copilot CLI looks for instructions in these locations (in order):
1. `.github/copilot-instructions.md` ← **Official location**
2. `.github/instructions/**/*.instructions.md`
3. `AGENTS.md` (in git root)
4. `$HOME/.copilot/copilot-instructions.md`

## Managing .github in the Vault

### Sync .github Directory

```bash
cd ~/git/YourRepo
~/FV-Copilot/sync-github.sh --dry-run  # Preview
~/FV-Copilot/sync-github.sh            # Apply
```

This creates: `repo/.github/` → `vault/repos/RepoName/dot.github/`

### Vault Structure

```
~/FV-Copilot/repos/YourRepo/
├── dot.copilot/                    # Your private context
├── dot.github/                     # Official GitHub files
│   ├── copilot-instructions.md     # Main Copilot instructions
│   ├── workflows/                  # GitHub Actions (if present)
│   └── CODEOWNERS                  # Code ownership (if present)
```

## What Goes in copilot-instructions.md

### Template
```markdown
# Copilot Instructions for YourRepo

## Project Overview
Brief description of what this project does

## Architecture
- Tech stack
- Key patterns
- Important conventions

## Development Workflow
- How to run locally
- How to test
- How to deploy

## AI Assistant Context
- Coding standards specific to this repo
- Common pitfalls to avoid
- Links to key documentation
```

### Best Practices
- ✅ Keep it concise (Copilot reads this on every request)
- ✅ Focus on repo-specific context
- ✅ Include patterns and conventions
- ✅ Mention important gotchas
- ❌ Don't duplicate general knowledge
- ❌ Avoid overly long documentation

## .github vs .copilot

**Use `.github/copilot-instructions.md` for:**
- Official team documentation
- Committed to repo (everyone sees it)
- General project context

**Use `.copilot/` for:**
- Private iteration and experiments
- Personal skills and patterns
- Module-specific deep context
- Draft content before promotion

## Workflow

1. **Draft in `.copilot/`** (vault, private iteration)
2. **Polish and promote to `.github/`** (official, committed)
3. **Edit both in Obsidian vault** (seamless)

## Example Structure

```
YourRepo/
├── .github/                        # Official (committed)
│   └── copilot-instructions.md     # Public team docs
├── .copilot/                       # Private (gitignored/symlinked)
│   ├── skills/                     # Your personal skills
│   ├── architecture.md             # Detailed notes
│   └── experiments.md              # Draft ideas
```

Both are managed in the vault as `dot.github/` and `dot.copilot/`!
