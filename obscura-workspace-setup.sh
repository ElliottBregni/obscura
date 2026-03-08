#!/usr/bin/env bash
# ~/.obscura/workspace setup
# Populates workspace/global (machine-wide) and workspace/local (FV-Platform-Main project)
# Safe to re-run: skips correct links, updates stale ones, reports missing targets.

GLOBAL="$HOME/.obscura/workspace/global"
LOCAL="$HOME/.obscura/workspace/local"
mkdir -p "$GLOBAL" "$LOCAL"

CREATED=0; SKIPPED=0; ERRORS=0; MISSING=0

make_link() {
  local DIR="$1" NAME="$2" TARGET="$3"
  local LINK="$DIR/$NAME"
  if [ ! -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
    printf "  %-45s  MISSING\n" "$NAME"; MISSING=$((MISSING+1)); return
  fi
  if [ -L "$LINK" ]; then
    local EX; EX=$(readlink "$LINK")
    if [ "$EX" = "$TARGET" ]; then
      printf "  %-45s  EXISTS\n" "$NAME"; SKIPPED=$((SKIPPED+1)); return
    fi
    rm -f "$LINK"
  fi
  if ln -s "$TARGET" "$LINK" 2>/dev/null; then
    printf "  %-45s  CREATED\n" "$NAME"; CREATED=$((CREATED+1))
  else
    printf "  %-45s  ERROR\n" "$NAME"; ERRORS=$((ERRORS+1))
  fi
}

# ── GLOBAL: machine-wide configs ───────────────────────────────────────────

echo ""; echo "== global: Claude =="
make_link "$GLOBAL" "claude"                     "$HOME/.claude"
make_link "$GLOBAL" "claude-json"                "$HOME/.claude.json"
make_link "$GLOBAL" "claude-desktop"             "$HOME/Library/Application Support/Claude"

echo ""; echo "== global: Obscura core =="
make_link "$GLOBAL" "obscura-settings"           "$HOME/.obscura/.claude/settings.json"
make_link "$GLOBAL" "obscura-settings-local"     "$HOME/.obscura/.claude/settings.local.json"
make_link "$GLOBAL" "obscura-history"            "$HOME/.obscura/.claude/history.jsonl"
make_link "$GLOBAL" "obscura-projects"           "$HOME/.obscura/.claude/projects"
make_link "$GLOBAL" "obscura-plans"              "$HOME/.obscura/.claude/plans"
make_link "$GLOBAL" "obscura-mcp-cache"          "$HOME/.obscura/.claude/mcp-needs-auth-cache.json"

echo ""; echo "== global: Obscura store =="
make_link "$GLOBAL" "agents"                     "$HOME/.obscura/agents"
make_link "$GLOBAL" "skills"                     "$HOME/.obscura/skills"
make_link "$GLOBAL" "mcp"                        "$HOME/.obscura/mcp"
make_link "$GLOBAL" "hooks"                      "$HOME/.obscura/hooks"
make_link "$GLOBAL" "sessions"                   "$HOME/.obscura/sessions"
make_link "$GLOBAL" "events-db"                  "$HOME/.obscura/events.db"
make_link "$GLOBAL" "qdrant"                     "$HOME/.obscura/qdrant"

echo ""; echo "== global: Copilot =="
make_link "$GLOBAL" "copilot"                    "$HOME/.copilot"
make_link "$GLOBAL" "copilot-audit"              "$HOME/.copilot/audit.log"
make_link "$GLOBAL" "copilot-compact"            "$HOME/.copilot/compact-session.py"
make_link "$GLOBAL" "copilot-intellij"           "$HOME/.config/github-copilot"
make_link "$GLOBAL" "copilot-intellij-mcp"       "$HOME/.config/github-copilot/intellij/mcp.json"
make_link "$GLOBAL" "copilot-intellij-inst"      "$HOME/.config/github-copilot/intellij/global-copilot-instructions.md"
make_link "$GLOBAL" "copilot-intellij-commit"    "$HOME/.config/github-copilot/intellij/global-git-commit-instructions.md"

echo ""; echo "== global: Other tools =="
make_link "$GLOBAL" "kiro-mcp"                   "$HOME/.kiro/settings/mcp.json"
make_link "$GLOBAL" "kiro-powers"                "$HOME/.kiro/powers"
make_link "$GLOBAL" "lmstudio-mcp"               "$HOME/.lmstudio/mcp.json"
make_link "$GLOBAL" "vscode-mcp"                 "$HOME/Library/Application Support/Code/User/mcp.json"
make_link "$GLOBAL" "cline-mcp"                  "$HOME/.config/cline/cline_mcp_config.json"

echo ""; echo "== global: Dev plugins =="
make_link "$GLOBAL" "plugins-skills-server"      "$HOME/dev/plugins/skills-server"
make_link "$GLOBAL" "plugins-agents"             "$HOME/dev/plugins/agents"

echo ""; echo "== global: Other projects =="
make_link "$GLOBAL" "agent-template"             "$HOME/git/agent-template"
make_link "$GLOBAL" "llm-skills-mcp"             "$HOME/dev/llm-skills/.github/mcp.json"
make_link "$GLOBAL" "copilot2"                   "$HOME/copilot2"

# ── LOCAL: FV-Platform-Main project ────────────────────────────────────────

echo ""; echo "== local: FV-Platform-Main configs =="
make_link "$LOCAL"  "claude-settings"            "$HOME/git/FV-Platform-Main/.claude/settings.local.json"
make_link "$LOCAL"  "github"                     "$HOME/git/FV-Platform-Main/.github"
make_link "$LOCAL"  "copilot-dir"                "$HOME/git/FV-Platform-Main/.copilot"

echo ""; echo "== local: agents & skills =="
make_link "$LOCAL"  "agents"                     "$HOME/.obscura/agents"
make_link "$LOCAL"  "agent-yaml"                 "$HOME/.obscura/agents/agent.yaml"
make_link "$LOCAL"  "agents-md"                  "$HOME/git/FV-Platform-Main/.github/AGENTS.MD"
make_link "$LOCAL"  "skills"                     "$HOME/.obscura/skills"

echo ""; echo "== local: MCP =="
make_link "$LOCAL"  "mcp-github"                 "$HOME/git/FV-Platform-Main/.github/mcp.json"
make_link "$LOCAL"  "mcp-copilot"                "$HOME/.obscura/mcp/mcp.json"
make_link "$LOCAL"  "mcp-obscura"                "$HOME/.obscura/mcp/obscura-copilot.json"

echo ""; echo "== local: hooks =="
make_link "$LOCAL"  "hooks-json"                 "$HOME/.obscura/hooks/hooks.json"
make_link "$LOCAL"  "hooks-dir"                  "$HOME/.obscura/hooks"

echo ""; echo "== local: setup =="
make_link "$LOCAL"  "workspace-setup.sh"         "$HOME/dev/obscura-main/obscura-workspace-setup.sh"
make_link "$LOCAL"  "workspace-migrate.sh"       "$HOME/dev/obscura-main/obscura-migrate.sh"
make_link "$LOCAL"  "workspace-migrate-hooks.sh" "$HOME/dev/obscura-main/obscura-migrate-agents-hooks.sh"

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "======================================================================"
printf "  Created: %-4s  Skipped: %-4s  Missing: %-4s  Errors: %s\n" \
  "$CREATED" "$SKIPPED" "$MISSING" "$ERRORS"
echo ""
echo "── global ($GLOBAL) ──"
ls "$GLOBAL"
echo ""
echo "── local ($LOCAL) ──"
ls "$LOCAL"
