"""Common path resolution helpers for Obscura runtime data."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_obscura_home(cwd: Path | None = None) -> Path:
    """Resolve Obscura home directory with sensible precedence."""
    env_home = os.environ.get("OBSCURA_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser().resolve()

    working_dir = (cwd or Path.cwd()).resolve()
    local_home = working_dir / ".obscura"
    if local_home.exists():
        return local_home

    return (Path.home() / ".obscura").resolve()


def resolve_obscura_mcp_dir(cwd: Path | None = None) -> Path:
    """Resolve directory containing MCP config files."""
    return resolve_obscura_home(cwd) / "mcp"


def resolve_obscura_skills_dir(cwd: Path | None = None) -> Path:
    """Resolve directory containing markdown skill documents."""
    return resolve_obscura_home(cwd) / "skills"


def resolve_agents_sessions_dir(cwd: Path | None = None) -> Path:
    """Resolve directory for synced agent sessions."""
    return resolve_obscura_home(cwd) / "agents" / "sessions"


def resolve_obscura_hooks_dir(cwd: Path | None = None) -> Path:
    """Resolve directory containing hook scripts."""
    return resolve_obscura_home(cwd) / "hooks"


def resolve_obscura_settings(cwd: Path | None = None) -> Path:
    """Resolve path to ``.obscura/settings.json``."""
    return resolve_obscura_home(cwd) / "settings.json"
