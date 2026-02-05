#!/bin/bash
# Sync .github directory to vault for editing
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
VAULT_GITHUB_PATH="$VAULT_PATH/repos/$REPO_NAME/dot.github"
REPO_GITHUB_PATH="$REPO_ROOT/.github"

echo "📁 Syncing: .github directory"
echo "  Vault: $VAULT_GITHUB_PATH"
echo "  Repo:  $REPO_GITHUB_PATH"
echo ""

# Check what exists
VAULT_EXISTS=false
REPO_EXISTS=false
REPO_IS_SYMLINK=false

if [ -d "$VAULT_GITHUB_PATH" ]; then
    VAULT_EXISTS=true
    echo "✓ Vault has .github files"
fi

if [ -L "$REPO_GITHUB_PATH" ]; then
    REPO_IS_SYMLINK=true
    echo "✓ Repo has symlink (already linked)"
elif [ -d "$REPO_GITHUB_PATH" ]; then
    REPO_EXISTS=true
    echo "✓ Repo has .github directory"
fi

echo ""

# Plan actions
if [ "$REPO_IS_SYMLINK" = true ]; then
    echo "⏭️  Already symlinked - nothing to do"
    exit 0
fi

# Merge strategy
if [ "$VAULT_EXISTS" = true ] && [ "$REPO_EXISTS" = true ]; then
    echo "🔀 MERGE: Both locations have .github"
    echo "   → Copy repo files to vault"
    echo "   → Remove repo directory"
    echo "   → Create symlink"
    
    if [ "$DRY_RUN" = false ]; then
        mkdir -p "$VAULT_GITHUB_PATH"
        cp -Rn "$REPO_GITHUB_PATH/"* "$VAULT_GITHUB_PATH/" 2>/dev/null || true
        cp -Rn "$REPO_GITHUB_PATH/".* "$VAULT_GITHUB_PATH/" 2>/dev/null || true
        rm -rf "$REPO_GITHUB_PATH"
        ln -s "$VAULT_GITHUB_PATH" "$REPO_GITHUB_PATH"
        echo "✅ Merged and linked"
    fi
    
elif [ "$REPO_EXISTS" = true ]; then
    echo "➡️  MOVE: Repo → Vault"
    echo "   → Move .github to vault as dot.github"
    echo "   → Create symlink"
    
    if [ "$DRY_RUN" = false ]; then
        mkdir -p "$(dirname "$VAULT_GITHUB_PATH")"
        mv "$REPO_GITHUB_PATH" "$VAULT_GITHUB_PATH"
        ln -s "$VAULT_GITHUB_PATH" "$REPO_GITHUB_PATH"
        echo "✅ Moved and linked"
    fi
    
elif [ "$VAULT_EXISTS" = true ]; then
    echo "⬅️  LINK: Vault → Repo"
    echo "   → Create symlink (vault already has content)"
    
    if [ "$DRY_RUN" = false ]; then
        ln -s "$VAULT_GITHUB_PATH" "$REPO_GITHUB_PATH"
        echo "✅ Linked"
    fi
    
else
    echo "➕ CREATE: New .github directory"
    echo "   → Create in vault with copilot-instructions.md"
    echo "   → Create symlink"
    
    if [ "$DRY_RUN" = false ]; then
        mkdir -p "$VAULT_GITHUB_PATH"
        
        # Create template copilot-instructions.md
        cat > "$VAULT_GITHUB_PATH/copilot-instructions.md" << 'EOF'
# Copilot Instructions for $(basename "$REPO_ROOT")

## Project Overview
[Brief description of what this project does]

## Architecture
[Key architectural patterns and decisions]

## Development Guidelines
- Coding standards
- Testing requirements
- Deployment process

## Common Tasks
### Running Tests
```bash
# Command to run tests
```

### Building
```bash
# Command to build
```

### Deployment
```bash
# Command to deploy
```

## Context for AI Assistants
- Use this space to provide context that helps Copilot understand your codebase
- Mention important patterns, conventions, or gotchas
- Link to key files or documentation
EOF
        
        ln -s "$VAULT_GITHUB_PATH" "$REPO_GITHUB_PATH"
        echo "✅ Created with template and linked"
    fi
fi

if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "🧪 Dry run complete - no changes made"
    echo "   Run without --dry-run to apply changes"
fi
