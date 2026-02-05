#!/bin/bash
# Bidirectional sync/merge script for .copilot directories
# Merges content from repo and vault, creates symlink
# Usage: ./sync-copilot.sh <relative-path> [--dry-run]
# Examples:
#   ./sync-copilot.sh .                    # Repo root
#   ./sync-copilot.sh platform/service     # Nested module
#   ./sync-copilot.sh . --dry-run          # Test without changes

set -e

DRY_RUN=false
if [[ "$2" == "--dry-run" ]] || [[ "$1" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "🧪 DRY RUN MODE - No changes will be made"
    echo ""
fi

VAULT_PATH="$HOME/FV-Copilot"
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)

if [ -z "$REPO_ROOT" ]; then
    echo "❌ Error: Must run from within a git repository"
    exit 1
fi

# Handle path argument
if [ -z "$1" ] || [[ "$1" == "--dry-run" ]]; then
    RELATIVE_PATH="."
else
    RELATIVE_PATH="$1"
fi

# Normalize path
if [ "$RELATIVE_PATH" == "." ]; then
    RELATIVE_PATH=""
fi

REPO_NAME=$(basename "$REPO_ROOT")
VAULT_BASE="$VAULT_PATH/repos/$REPO_NAME"

if [ -z "$RELATIVE_PATH" ]; then
    VAULT_DOT_PATH="$VAULT_BASE/dot.copilot"
    REPO_DOT_PATH="$REPO_ROOT/.copilot"
    DISPLAY_PATH="(repo root)"
else
    VAULT_DOT_PATH="$VAULT_BASE/$RELATIVE_PATH/dot.copilot"
    REPO_DOT_PATH="$REPO_ROOT/$RELATIVE_PATH/.copilot"
    DISPLAY_PATH="$RELATIVE_PATH"
fi

echo "📁 Syncing: $DISPLAY_PATH"
echo "  Vault: $VAULT_DOT_PATH"
echo "  Repo:  $REPO_DOT_PATH"
echo ""

# Check what exists
VAULT_EXISTS=false
REPO_EXISTS=false
REPO_IS_SYMLINK=false

if [ -d "$VAULT_DOT_PATH" ]; then
    VAULT_EXISTS=true
    VAULT_FILES=$(find "$VAULT_DOT_PATH" -type f 2>/dev/null | wc -l | tr -d ' ')
    echo "✓ Vault has $VAULT_FILES files"
fi

if [ -L "$REPO_DOT_PATH" ]; then
    REPO_IS_SYMLINK=true
    echo "✓ Repo has symlink (already linked)"
elif [ -d "$REPO_DOT_PATH" ]; then
    REPO_EXISTS=true
    REPO_FILES=$(find "$REPO_DOT_PATH" -type f 2>/dev/null | wc -l | tr -d ' ')
    echo "✓ Repo has $REPO_FILES files"
fi

echo ""

# Plan actions
if [ "$REPO_IS_SYMLINK" = true ]; then
    echo "⏭️  Already symlinked - nothing to do"
    exit 0
fi

# Merge strategy
if [ "$VAULT_EXISTS" = true ] && [ "$REPO_EXISTS" = true ]; then
    echo "🔀 MERGE: Both locations have content"
    echo "   → Copy repo files to vault"
    echo "   → Remove repo directory"
    echo "   → Create symlink"
    
    if [ "$DRY_RUN" = false ]; then
        mkdir -p "$VAULT_DOT_PATH"
        cp -Rn "$REPO_DOT_PATH/"* "$VAULT_DOT_PATH/" 2>/dev/null || true
        cp -Rn "$REPO_DOT_PATH/".* "$VAULT_DOT_PATH/" 2>/dev/null || true
        rm -rf "$REPO_DOT_PATH"
        ln -s "$VAULT_DOT_PATH" "$REPO_DOT_PATH"
        echo "✅ Merged and linked"
    fi
    
elif [ "$REPO_EXISTS" = true ]; then
    echo "➡️  MOVE: Repo → Vault"
    echo "   → Move repo directory to vault as dot.copilot"
    echo "   → Create symlink"
    
    if [ "$DRY_RUN" = false ]; then
        mkdir -p "$(dirname "$VAULT_DOT_PATH")"
        mv "$REPO_DOT_PATH" "$VAULT_DOT_PATH"
        ln -s "$VAULT_DOT_PATH" "$REPO_DOT_PATH"
        echo "✅ Moved and linked"
    fi
    
elif [ "$VAULT_EXISTS" = true ]; then
    echo "⬅️  LINK: Vault → Repo"
    echo "   → Create symlink (vault already has content)"
    
    if [ "$DRY_RUN" = false ]; then
        ln -s "$VAULT_DOT_PATH" "$REPO_DOT_PATH"
        echo "✅ Linked"
    fi
    
else
    echo "➕ CREATE: New empty directory"
    echo "   → Create in vault"
    echo "   → Create symlink"
    
    if [ "$DRY_RUN" = false ]; then
        mkdir -p "$VAULT_DOT_PATH"
        ln -s "$VAULT_DOT_PATH" "$REPO_DOT_PATH"
        echo "✅ Created and linked"
    fi
fi

if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "🧪 Dry run complete - no changes made"
    echo "   Run without --dry-run to apply changes"
fi
