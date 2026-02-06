# VaultHopper



Roadmap: stabilize an MVP (polished CLI, pip/standalone binaries, docs, example prompt pipelines, Obsidian symlink
  manager), then build the recursive-agent prototype with strict loop termination, caching, cost controls and
  observability; next add integrations (Docker image, tiny web dashboard, analytics), team features (SSO, audit logs),
  SDKs/IDE plugins and a prompt/plugin marketplace for extensibility.
  Monetization: freemium (use-limited free tier) → Pro per-user subscription + Team tier (flat fee + per-seat or usage),
  enterprise contracts (on‑prem or managed, SSO, SLA), plus paid add‑ons (marketplace listings, priority support,
  analytics, white‑label licensing) and usage-based billing for high-volume customers.
  Go-to-market: publish to PyPI/GitHub, one‑page site + demo video, targeted developer content (tutorials, HN/Reddit
  posts, newsletter outreach), early-access trials/webinars and convert power users to paid via time‑limited discounts
  and case-study-driven outreach.


**Multi-agent context management system** for code repositories. Keep skills, instructions, and architectural knowledge in one place, synced across multiple AI agents (GitHub Copilot, Claude, etc.) and repositories.

---

## 🎯 What This Is

A **single source of truth** for LLM context that:
- ✅ Works with **any AI agent** (Copilot, Claude, Cursor, or custom)
- ✅ Syncs to **any number of code repositories**
- ✅ Uses **symlinks** for zero-copy, instant updates
- ✅ Supports **universal** and **agent-specific** overrides
- ✅ Keeps **vault separate** from code repos (no PRs for context iteration)
- ✅ Works with **Obsidian** or any Markdown editor

---

## 🚀 Quick Start

### 1. **Add a Repository**
```bash
cd ~/FV-Copilot
# Add repo path to repos/INDEX.md
echo "~/git/YourRepo" >> repos/INDEX.md
```

### 2. **Sync**
```bash
# Sync in symlink mode (instant, zero-copy)
python3 sync.py --repo ~/git/YourRepo --agent copilot --mode symlink
```

### 3. **Edit & Iterate**
- Edit files in `~/FV-Copilot/skills/` or `instructions/`
- Changes instantly appear in all linked repos via symlinks
- No git commits needed until you're ready

---

## 📂 Structure

### **Core Directories**

| Directory | Purpose | Sync Target |
|-----------|---------|-------------|
| **`skills/`** | Skill files for LLMs | → `repo/.github/skills/` (copilot)<br>→ `repo/.claude/skills/` (claude) |
| **`instructions/`** | Instruction sets for LLMs | → `repo/.github/instructions/` (copilot)<br>→ `repo/.claude/instructions/` (claude) |
| **`agents/`** | Agent registry (`INDEX.md`) | Reference only |
| **`repos/`** | Repository index (`INDEX.md`) | Links to code repos |
| **`docs/`** | Vault documentation | Internal only |
| **`git-hooks/`** | Shared git hooks | → `repo/.git/hooks/` |

### **Universal vs Agent-Specific Files**

The vault supports **three routing patterns** to manage shared and agent-specific content:

#### 1️⃣ **Universal Files** (Shared Across All Agents)
```
skills/
├── setup.md              # Used by all agents
├── git-workflow.md       # Shared everywhere
└── testing.md            # Universal testing guide
```

#### 2️⃣ **Directory-Level Agent Overrides**
```
skills/
├── setup.md              # Universal fallback
├── skills.copilot/       # Copilot-specific directory
│   ├── api-design.md     # Copilot only
│   └── python.md         # Overrides universal python.md
└── skills.claude/        # Claude-specific directory
    └── database.md       # Claude only
```

#### 3️⃣ **Nested File Overrides** (Recommended)
```
instructions/
├── x.md                  # Universal version
├── x.copilot.md          # Copilot override (same directory)
├── x.claude.md           # Claude override (same directory)
├── y.copilot.md          # Copilot-only (no universal)
└── z.md                  # Universal only (no overrides)
```

**Priority Rules:**
- Directory-level (`skills.copilot/`) > Nested file (`skills/x.copilot.md`) > Universal (`skills/x.md`)
- Agent-specific always wins over universal
- Missing agent-specific file? Falls back to universal

See [docs/AGENT-ROUTING.md](docs/AGENT-ROUTING.md) for full details.

---

## 🔄 Sync Modes

### **Symlink Mode** (Recommended)
Zero-copy, instant sync with multi-agent support. Changes in vault appear immediately in repos.

```bash
# Link for a specific agent
python3 sync.py --repo ~/git/MyRepo --agent copilot --mode symlink

# Link for all registered agents
python3 sync.py --repo ~/git/MyRepo --mode symlink
```

**Features:**
- Multi-agent routing (copilot → `.github/`, claude → `.claude/`)
- Multiple agents per repo (both `.github/` and `.claude/` can coexist)
- Recursive directory-matching sync (vault tree → repo tree)
- Broken symlink detection and auto-repair
- Agent validation (ensures agent is registered before linking)

**Pros:** Instant, no duplication, edit anywhere, multi-agent support
**Cons:** Git operations can break symlinks (auto-repair available via git hooks)

### **Copy Mode**
One-way file copy from vault to repo.

```bash
python3 sync.py --repo ~/git/MyRepo --agent copilot --mode copy
```

**Pros:** Git-safe, no symlink issues
**Cons:** Manual sync required for changes

### **Watch Mode**
Continuous background sync using fswatch. Auto-syncs when vault files change.

```bash
python3 sync.py --watch
```

**Pros:** Hands-free, re-syncs on every vault edit
**Cons:** Requires fswatch (`brew install fswatch`)

---

## 📚 Documentation

### **Getting Started**
- [📦 Installation Guide](docs/INSTALL.md) - Full setup instructions
- [⚡ Quick Start](docs/QUICKSTART.md) - Get running in 5 minutes
- [🔗 GitHub Integration](docs/GITHUB-INTEGRATION.md) - Sync strategies
- [📝 No Obsidian?](docs/NO-OBSIDIAN.md) - Use any Markdown editor

### **Advanced Topics**
- [🎭 Agent Routing](docs/AGENT-ROUTING.md) - Multi-agent patterns & priority rules
- [🤖 Auto-Sync](docs/AUTO-SYNC.md) - Background sync with launchd
- [🔧 MCP Config](docs/MCP-README.md) - Model Context Protocol setup
- [📊 Migration Notes](docs/MIGRATION-NOTES.md) - Upgrading from old structures

---

## 🛠️ Common Workflows

### **Add a New Skill**
```bash
# Create universal skill (all agents)
echo "# Python Best Practices" > skills/python.md

# Or create agent-specific skill
echo "# Copilot Python Tips" > skills/python.copilot.md
```

### **Override an Instruction for One Agent**
```bash
# Universal instruction exists at instructions/setup.md
# Create Copilot-specific override:
cp instructions/setup.md instructions/setup.copilot.md
# Edit setup.copilot.md - only Copilot sees changes
```

### **Sync Multiple Repos**
```bash
# List all repos in repos/INDEX.md
cat repos/INDEX.md
# Output:
# ~/git/FV-Platform-Main
# ~/git/OtherProject

# Sync all repos for Copilot
python3 sync.py --agent copilot --mode symlink

# Sync all repos for all registered agents
python3 sync.py --mode symlink

# Sync specific repo for specific agent
python3 sync.py --repo ~/git/MyRepo --agent copilot --mode symlink
```

### **Test Before Committing**
```bash
# 1. Link repo in symlink mode
python3 sync.py --repo ~/git/MyRepo --agent copilot --mode symlink

# 2. Edit skills/instructions in vault
# 3. Test in your IDE (changes are live via symlinks)
# 4. When ready, convert symlinks to real files:
python3 sync.py --merge --repo ~/git/MyRepo

# 5. Commit in repo
cd ~/git/MyRepo && git add .github/ && git commit
```

---

## 🎭 Multi-Agent Support

Register agents in `agents/INDEX.md`:
```markdown
# Active Agents

- copilot    → .github/
- claude     → .claude/
- cursor     → .cursor/
```

Each agent gets its own target path in repositories. Files route automatically based on agent suffix or directory name.

**Example:**
- `skills.copilot/api.md` → `repo/.github/skills/api.md`
- `skills.claude/api.md` → `repo/.claude/skills/api.md`
- `skills/api.md` → Both agents see it (universal)

---

## 🔍 Troubleshooting

### **Symlinks Not Working?**
```bash
# Check if symlinks exist
ls -la ~/git/MyRepo/.github/

# Re-link if broken
python3 sync.py --repo ~/git/MyRepo --agent copilot --mode symlink
```

### **Agent Not Seeing Files?**
1. Check agent is registered in `agents/INDEX.md`
2. Verify symlink exists: `ls -la ~/git/MyRepo/.github` (copilot) or `~/.claude` (claude)
3. Check symlink target: `readlink ~/git/MyRepo/.github`
4. Verify file naming: `filename.copilot.md` or `directory.copilot/`
5. Check priority: directory-level > nested file > universal
6. See [Agent Routing Docs](docs/AGENT-ROUTING.md#troubleshooting)

### **Wrong Agent Path?**
Each agent has a specific target path:
- `copilot` → `.github/`
- `claude` → `.claude/`
- `cursor` → `.cursor/`
- Custom agents → `.agent-name/`

Check the symlink is pointing to the right directory for your agent.

### **Git Rebase Broke Symlinks?**
```bash
# Auto-repair with post-merge hook
cp git-hooks/post-merge ~/git/MyRepo/.git/hooks/
chmod +x ~/git/MyRepo/.git/hooks/post-merge

# Or manually re-link
python3 sync.py --repo ~/git/MyRepo --agent copilot --mode symlink
```

---

## 🧪 Testing

After syncing, verify what each agent sees:

```bash
# Test Copilot
ls -la ~/git/MyRepo/.github/skills/
cat ~/git/MyRepo/.github/skills/python.md

# Test Claude
ls -la ~/git/MyRepo/.claude/skills/
cat ~/git/MyRepo/.claude/skills/python.md
```

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      FV-Copilot Vault                        │
│  (Single Source of Truth)                                    │
│                                                               │
│  skills/                instructions/           agents/      │
│  ├── universal.md       ├── x.md                ├── INDEX.md │
│  ├── skill.copilot.md  ├── x.copilot.md        └── copilot  │
│  └── skill.claude.md   └── x.claude.md             claude    │
└───────────────┬─────────────────────────────────────────────┘
                │
                │ sync.py (symlink/copy/watch)
                │
        ┌───────┴────────┬────────────────┐
        ▼                ▼                ▼
  ┌─────────┐      ┌─────────┐      ┌─────────┐
  │ Repo A  │      │ Repo B  │      │ Repo C  │
  │         │      │         │      │         │
  │.github/ │      │.github/ │      │.claude/ │
  │.claude/ │      │.cursor/ │      │.github/ │
  └─────────┘      └─────────┘      └─────────┘
```

**Key Principles:**
- Vault is the **source of truth**
- Repos are **targets** (symlinks or copies)
- Agent routing is **automatic** (based on file naming)
- Priority is **deterministic** (directory > nested > universal)

---

## 📜 License

MIT License - See repository for details.

---

## 🤝 Contributing

See [docs/MIGRATION-NOTES.md](docs/MIGRATION-NOTES.md) for development patterns and best practices.

---

## 📞 Support

- **Documentation:** [docs/](docs/) folder
- **Agent Routing:** [AGENT-ROUTING.md](docs/AGENT-ROUTING.md)
- **Auto-Sync:** [AUTO-SYNC.md](docs/AUTO-SYNC.md)
- **MCP Setup:** [MCP-README.md](docs/MCP-README.md)

---

**Built for developers who iterate fast on LLM context without spamming PRs.** 🚀
