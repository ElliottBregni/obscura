"""Tool handlers for the obscura-github plugin.

Each function corresponds to a tool declared in plugin.yaml and is
referenced via its ``handler`` field (e.g. ``obscura_github.tools:search_repo``).
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any


def _gh(*args: str) -> str:
    """Run ``gh`` CLI and return stdout."""
    token = os.environ.get("GITHUB_TOKEN", "")
    env = {**os.environ, "GH_TOKEN": token} if token else None
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def search_repo(owner: str, repo: str, query: str) -> str:
    """Search repository contents by keyword."""
    return _gh("search", "code", query, "--repo", f"{owner}/{repo}", "--json", "path,textMatches", "--limit", "20")


def get_file(owner: str, repo: str, path: str, ref: str = "HEAD") -> str:
    """Get file contents from a repository."""
    return _gh("api", f"/repos/{owner}/{repo}/contents/{path}?ref={ref}",
               "--jq", ".content", "-H", "Accept: application/vnd.github.v3+json")


def list_branches(owner: str, repo: str) -> str:
    """List branches of a repository."""
    return _gh("api", f"/repos/{owner}/{repo}/branches", "--jq", ".[].name")


def comment_pr(owner: str, repo: str, pr_number: int, body: str) -> str:
    """Add a comment to a pull request."""
    return _gh("pr", "comment", str(pr_number), "--repo", f"{owner}/{repo}", "--body", body)
