#!/bin/bash
# Sync .github directories to vault for editing (root + nested modules)
# 
# Creates symlinks: repo/.github → vault/repos/RepoName/
# Only creates dedicated .github where code directories exist
# Vault-only folders (skills/, instructions/) accessible via parent .github
#
# Usage: ./sync-github.sh [--dry-run]

set -e

DRY_RUN=false
if [[ "$1" == "--dry-run" ]]; then
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

REPO_NAME=$(basename "$REPO_ROOT")
VAULT_REPO_BASE="$VAULT_PATH/repos/$REPO_NAME"

# Function to sync a single .github location
sync_github_location() {
    local repo_path="$1"
    local vault_path="$2"
    local relative_path="$3"
    
    echo ""
    echo "📁 Syncing: $relative_path"
    echo "  Vault: $vault_path"
    echo "  Repo:  $repo_path"
    
    # Check what exists
    local vault_exists=false
    local repo_exists=false
    local repo_is_symlink=false
    
    if [ -d "$vault_path" ] && [ -n "$(ls -A "$vault_path" 2>/dev/null)" ]; then
        vault_exists=true
        echo "  ✓ Vault has content"
    fi
    
    if [ -L "$repo_path" ]; then
        repo_is_symlink=true
        echo "  ✓ Already symlinked"
    elif [ -d "$repo_path" ]; then
        repo_exists=true
        echo "  ✓ Repo has .github directory"
    fi
    
    # Skip if already linked
    if [ "$repo_is_symlink" = true ]; then
        echo "  ⏭️  Nothing to do"
        return 0
    fi
    
    # Handle different scenarios
    if [ "$vault_exists" = true ] && [ "$repo_exists" = true ]; then
        echo "  🔀 MERGE: Copy repo → vault, then link"
        if [ "$DRY_RUN" = false ]; then
            cp -Rn "$repo_path/"* "$vault_path/" 2>/dev/null || true
            rm -rf "$repo_path"
            ln -s "$vault_path" "$repo_path"
            echo "  ✅ Merged and linked"
        fi
        
    elif [ "$repo_exists" = true ]; then
        echo "  ➡️  MOVE: Repo → vault, then link"
        if [ "$DRY_RUN" = false ]; then
            mkdir -p "$(dirname "$vault_path")"
            mv "$repo_path" "$vault_path"
            ln -s "$vault_path" "$repo_path"
            echo "  ✅ Moved and linked"
        fi
        
    elif [ "$vault_exists" = true ]; then
        echo "  ⬅️  LINK: Create symlink to vault"
        if [ "$DRY_RUN" = false ]; then
            ln -s "$vault_path" "$repo_path"
            echo "  ✅ Linked"
        fi
    else
        echo "  ⏭️  No content - skipping"
    fi
}

# Sync root .github
echo "🔄 Scanning for .github directories..."
sync_github_location "$REPO_ROOT/.github" "$VAULT_REPO_BASE" ".github (root)"

# Find all nested directories in vault that should have .github symlinks
# Only sync if directory contains actual content files (*.md, etc), not just subdirectories
if [ -d "$VAULT_REPO_BASE" ]; then
    while IFS= read -r -d '' vault_dir; do
        # Get relative path from vault repo base
        rel_path="${vault_dir#$VAULT_REPO_BASE/}"
        
        # Skip if it's the root (already handled)
        if [ "$rel_path" = "$VAULT_REPO_BASE" ] || [ -z "$rel_path" ]; then
            continue
        fi
        
        # Corresponding repo location
        repo_dir="$REPO_ROOT/$rel_path"
        
        # Only sync if:
        # 1. Vault dir has actual content FILES (*.md, *.json)
        # 2. The corresponding directory exists in the actual repo (not vault-only)
        if [ -d "$repo_dir" ] && [ -n "$(find "$vault_dir" -maxdepth 1 -type f \( -name "*.md" -o -name "*.json" \) 2>/dev/null)" ]; then
            sync_github_location "$repo_dir/.github" "$vault_dir" ".github ($rel_path)"
        fi
    done < <(find "$VAULT_REPO_BASE" -type d -print0)
fi

echo ""
echo "✅ Sync complete!"

if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "🧪 Dry run complete - no changes made"
    echo "   Run without --dry-run to apply changes"
fi
