#!/bin/bash
# Remove agent symlinks from managed repos
# Delegates to sync.py --clean for per-file symlink support
#
# Usage:
#   ./remove-links.sh                    # Dry-run (all repos)
#   ./remove-links.sh --force            # Actually remove
#   ./remove-links.sh --repo RepoName    # Specific repo only

set -e

VAULT_PATH="$HOME/FV-Copilot"
DRY_RUN="--dry-run"
REPO_ARG=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --force) DRY_RUN=""; shift ;;
        --repo) REPO_ARG="--repo $2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

python3 "$VAULT_PATH/sync.py" --clean $DRY_RUN $REPO_ARG
