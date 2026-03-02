#!/usr/bin/env bash
set -euo pipefail

echo "=== Move .github/hooks/ → ~/.obscura/hooks/ ==="
HOOKS_SRC="$HOME/git/FV-Platform-Main/.github/hooks"
HOOKS_DST="$HOME/.obscura/hooks"

if [ -d "$HOOKS_SRC" ] && [ ! -L "$HOOKS_SRC" ]; then
  cp -r "$HOOKS_SRC"/. "$HOOKS_DST"/
  rm -rf "$HOOKS_SRC"
  ln -s "$HOOKS_DST" "$HOOKS_SRC"
  echo "  ✓ .github/hooks → ~/.obscura/hooks"
elif [ -L "$HOOKS_SRC" ]; then
  echo "  ↷ already a symlink, skipping"
else
  echo "  ✗ not found: $HOOKS_SRC"
fi

echo ""
echo "=== Move .github/agents/ → ~/.obscura/agents/ ==="
AGENTS_SRC="$HOME/git/FV-Platform-Main/.github/agents"
AGENTS_DST="$HOME/.obscura/agents"

if [ -d "$AGENTS_SRC" ] && [ ! -L "$AGENTS_SRC" ]; then
  cp -r "$AGENTS_SRC"/. "$AGENTS_DST"/
  rm -rf "$AGENTS_SRC"
  ln -s "$AGENTS_DST" "$AGENTS_SRC"
  echo "  ✓ .github/agents → ~/.obscura/agents"
elif [ -L "$AGENTS_SRC" ]; then
  echo "  ↷ already a symlink, skipping"
else
  echo "  ✗ not found: $AGENTS_SRC"
fi

echo ""
echo "=== .github/ after ==="
ls -la ~/git/FV-Platform-Main/.github/

echo ""
echo "=== ~/.obscura/hooks/ ==="
ls -la ~/.obscura/hooks/

echo ""
echo "=== ~/.obscura/agents/ (count) ==="
ls ~/.obscura/agents/ | wc -l

echo ""
echo "=== ~/.obscura/ top-level ==="
ls -la ~/.obscura/
