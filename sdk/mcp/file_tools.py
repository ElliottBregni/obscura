"""
sdk.mcp.file_tools — Sandboxed file read and search for MCP tools.

Provides file operations that are restricted to allowed directories,
preventing path traversal and access to sensitive system files.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Sandboxing
# ---------------------------------------------------------------------------

_DEFAULT_ALLOWED_ROOTS: list[str] = [
    os.path.expanduser("~"),
]

_BLOCKED_PATTERNS: list[str] = [
    "*.env",
    "*.pem",
    "*.key",
    "*credentials*",
    "*secrets*",
    "*.ssh/*",
    "*/.git/objects/*",
]


def _is_blocked(path: Path) -> bool:
    """Check if a path matches any blocked pattern."""
    path_str = str(path)
    for pattern in _BLOCKED_PATTERNS:
        if fnmatch.fnmatch(path_str, pattern):
            return True
        if fnmatch.fnmatch(path.name, pattern):
            return True
    return False


def _resolve_safe(
    path_str: str,
    allowed_roots: list[str] | None = None,
) -> Path:
    """Resolve a path and verify it's within allowed roots.

    Raises:
        ValueError: If path is outside allowed roots or is blocked.
    """
    roots = allowed_roots or _DEFAULT_ALLOWED_ROOTS
    resolved = Path(path_str).expanduser().resolve()

    # Check allowed roots
    in_allowed = any(
        str(resolved).startswith(str(Path(r).resolve())) for r in roots
    )
    if not in_allowed:
        raise ValueError(
            f"Path {path_str!r} is outside allowed directories"
        )

    # Check blocked patterns
    if _is_blocked(resolved):
        raise ValueError(f"Path {path_str!r} matches a blocked pattern")

    return resolved


# ---------------------------------------------------------------------------
# File read
# ---------------------------------------------------------------------------


def read_file(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    *,
    allowed_roots: list[str] | None = None,
    max_bytes: int = 1_000_000,
) -> dict[str, Any]:
    """Read file content with optional line range.

    Args:
        path: File path to read.
        start_line: 1-based inclusive start line.
        end_line: 1-based inclusive end line.
        allowed_roots: Override default allowed root directories.
        max_bytes: Maximum file size to read (default 1MB).

    Returns:
        Dict with ``path``, ``content``, ``total_lines``, ``start_line``,
        ``end_line``, and ``truncated``.
    """
    resolved = _resolve_safe(path, allowed_roots)

    if not resolved.exists():
        return {"error": f"File not found: {path}"}

    if not resolved.is_file():
        return {"error": f"Not a file: {path}"}

    # Size guard
    size = resolved.stat().st_size
    if size > max_bytes:
        return {
            "error": f"File too large ({size:,} bytes > {max_bytes:,} max)",
            "path": str(resolved),
            "size": size,
        }

    text = resolved.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    total = len(lines)

    # Apply line range (1-based)
    s = (start_line - 1) if start_line and start_line > 0 else 0
    e = end_line if end_line and end_line > 0 else total
    selected = lines[s:e]

    content = "".join(selected)
    truncated = len(selected) < total

    return {
        "path": str(resolved),
        "content": content,
        "total_lines": total,
        "start_line": s + 1,
        "end_line": min(e, total),
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# File search
# ---------------------------------------------------------------------------


def search_files(
    query: str,
    glob_pattern: str = "*",
    *,
    root: str | None = None,
    allowed_roots: list[str] | None = None,
    limit: int = 20,
    max_file_size: int = 500_000,
) -> dict[str, Any]:
    """Search files by glob pattern and/or content.

    Args:
        query: Text to search for in file contents.
        glob_pattern: Glob pattern for filtering files (default ``"*"``).
        root: Directory to start searching from (default cwd).
        allowed_roots: Override default allowed root directories.
        limit: Maximum number of results.
        max_file_size: Skip files larger than this.

    Returns:
        Dict with ``results`` list and ``count``.
    """
    search_root = Path(root or os.getcwd()).expanduser().resolve()

    # Validate search root
    roots = allowed_roots or _DEFAULT_ALLOWED_ROOTS
    in_allowed = any(
        str(search_root).startswith(str(Path(r).resolve())) for r in roots
    )
    if not in_allowed:
        return {"error": f"Search root {root!r} is outside allowed directories"}

    results: list[dict[str, Any]] = []

    for p in search_root.rglob(glob_pattern):
        if len(results) >= limit:
            break

        if not p.is_file():
            continue

        if _is_blocked(p):
            continue

        # Size guard
        try:
            if p.stat().st_size > max_file_size:
                continue
        except OSError:
            continue

        # Content search
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError):
            continue

        # Find matching lines
        matches: list[dict[str, Any]] = []
        for i, line in enumerate(text.splitlines(), 1):
            if query.lower() in line.lower():
                matches.append({"line": i, "text": line.rstrip()})
                if len(matches) >= 5:  # Cap matches per file
                    break

        if matches:
            results.append({
                "path": str(p),
                "matches": matches,
            })

    return {
        "results": results,
        "count": len(results),
        "query": query,
        "glob": glob_pattern,
        "root": str(search_root),
    }
