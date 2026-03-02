#!/usr/bin/env bash
set -euo pipefail

echo "=== Step 1: Create new ~/.obscura dirs ==="
mkdir -p ~/.obscura/agents
mkdir -p ~/.obscura/skills
mkdir -p ~/.obscura/mcp
mkdir -p ~/.obscura/hooks
mkdir -p ~/.obscura/sessions
mkdir -p ~/.obscura/workspace/global
mkdir -p ~/.obscura/workspace/local
echo "  ✓ Directories created"

echo ""
echo "=== Step 2: Move .copilot/skills → ~/.obscura/skills/ ==="
SKILLS_SRC="$HOME/git/FV-Platform-Main/.copilot/skills"
SKILLS_DST="$HOME/.obscura/skills"

if [ -d "$SKILLS_SRC" ] && [ ! -L "$SKILLS_SRC" ]; then
  cp -r "$SKILLS_SRC"/. "$SKILLS_DST"/
  rm -rf "$SKILLS_SRC"
  ln -s "$SKILLS_DST" "$SKILLS_SRC"
  echo "  ✓ Skills moved and symlinked: .copilot/skills → ~/.obscura/skills"
elif [ -L "$SKILLS_SRC" ]; then
  echo "  ↷ .copilot/skills already a symlink, skipping"
else
  echo "  ✗ Source not found: $SKILLS_SRC"
fi

echo ""
echo "=== Step 3: Move .copilot/mcp/ → ~/.obscura/mcp/ ==="
MCP_SRC="$HOME/git/FV-Platform-Main/.copilot/mcp"
MCP_DST="$HOME/.obscura/mcp"

if [ -d "$MCP_SRC" ] && [ ! -L "$MCP_SRC" ]; then
  cp -r "$MCP_SRC"/. "$MCP_DST"/
  rm -rf "$MCP_SRC"
  ln -s "$MCP_DST" "$MCP_SRC"
  echo "  ✓ MCP dir moved and symlinked: .copilot/mcp → ~/.obscura/mcp"
elif [ -L "$MCP_SRC" ]; then
  echo "  ↷ .copilot/mcp already a symlink, skipping"
else
  echo "  ✗ Source not found: $MCP_SRC"
fi

echo ""
echo "=== Step 4: Move providers/.copilot/mcp-config.json → ~/.obscura/mcp/obscura-copilot.json ==="
COPILOT_MCP_SRC="$HOME/.obscura/providers/.copilot/mcp-config.json"
COPILOT_MCP_DST="$HOME/.obscura/mcp/obscura-copilot.json"

if [ -f "$COPILOT_MCP_SRC" ] && [ ! -L "$COPILOT_MCP_SRC" ]; then
  cp "$COPILOT_MCP_SRC" "$COPILOT_MCP_DST"
  rm "$COPILOT_MCP_SRC"
  ln -s "$COPILOT_MCP_DST" "$COPILOT_MCP_SRC"
  echo "  ✓ obscura-copilot.json moved and symlinked back"
elif [ -L "$COPILOT_MCP_SRC" ]; then
  echo "  ↷ mcp-config.json already a symlink, skipping"
else
  echo "  ✗ Source not found: $COPILOT_MCP_SRC"
fi

echo ""
echo "=== Step 5: Symlink ~/.obscura/sessions → providers/.claude/debug ==="
SESSIONS_DST="$HOME/.obscura/sessions"
DEBUG_SRC="$HOME/.obscura/providers/.claude/debug"

if [ -d "$DEBUG_SRC" ]; then
  rmdir "$SESSIONS_DST" 2>/dev/null || true
  if [ ! -L "$SESSIONS_DST" ]; then
    ln -s "$DEBUG_SRC" "$SESSIONS_DST"
    echo "  ✓ ~/.obscura/sessions → providers/.claude/debug"
  else
    echo "  ↷ sessions symlink already exists"
  fi
else
  echo "  ✗ debug dir not found: $DEBUG_SRC"
fi

echo ""
echo "=== Verification ==="
echo ""
echo "~/.obscura/ top-level:"
ls -la ~/.obscura/
echo ""
echo ".copilot/ after migration:"
ls -la ~/git/FV-Platform-Main/.copilot/
echo ""
echo "~/.obscura/mcp/ contents:"
ls -la ~/.obscura/mcp/
echo ""
echo "~/.obscura/skills/ (count):"
ls ~/.obscura/skills/ | wc -l
