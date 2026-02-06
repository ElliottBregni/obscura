#!/bin/bash
# Smart merge: If agent target dir exists as real directory, merge into vault, then relink
# Delegates to sync.py --merge for per-file symlink support
#
# Usage:
#   ./merge-and-relink.sh                    # Dry-run (all repos)
#   ./merge-and-relink.sh --force            # Actually merge and relink
#   ./merge-and-relink.sh --repo RepoName    # Specific repo only

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

python3 "$VAULT_PATH/sync.py" --merge $DRY_RUN $REPO_ARG
