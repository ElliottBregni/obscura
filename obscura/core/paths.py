"""Common path resolution helpers for Obscura runtime data."""

from __future__ import annotations

import os
from pathlib import Path

from obscura.agent.definitions import _BUILTIN_DIR  # pyright: ignore[reportPrivateUsage]


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


def resolve_obscura_global_home() -> Path:
    """Resolve the global ``~/.obscura/`` directory (ignoring local overrides).

    Respects ``OBSCURA_HOME`` env var, otherwise returns ``~/.obscura/``.
    Use this when you always need the user-global directory, e.g. for
    user-authored plugins that should be available in every project.
    """
    env_home = os.environ.get("OBSCURA_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".obscura").resolve()


def resolve_obscura_plugins_dir(cwd: Path | None = None) -> Path:
    """Resolve ``.obscura/plugins/`` directory for the active home."""
    return resolve_obscura_home(cwd) / "plugins"


def resolve_obscura_specs_dir(cwd: Path | None = None) -> Path:
    """Resolve ``.obscura/specs/`` directory for declarative spec files."""
    return resolve_obscura_home(cwd) / "specs"


def resolve_obscura_state_dir(cwd: Path | None = None) -> Path:
    """Resolve ``.obscura/state/`` directory for runtime state files."""
    return resolve_obscura_home(cwd) / "state"


def resolve_obscura_evals_dir(cwd: Path | None = None) -> Path:
    """Resolve ``.obscura/evals/`` directory for eval suite files."""
    return resolve_obscura_home(cwd) / "evals"


def resolve_obscura_output_dir(cwd: Path | None = None) -> Path:
    """Resolve ``.obscura/output/`` directory for agent-generated files.

    This is the default destination for files written by tools when the
    LLM provides a relative path.  Keeps generated content (reports,
    exports, etc.) out of the project working directory.
    """
    return resolve_obscura_home(cwd) / "output"


# ---------------------------------------------------------------------------
# Multi-home helpers (global + local merging)
# ---------------------------------------------------------------------------


def resolve_all_obscura_homes(cwd: Path | None = None) -> tuple[Path, Path]:
    """Return ``(local, global)`` ``.obscura/`` directories.

    The local directory is the project-level ``.obscura/`` under *cwd*.
    The global directory is ``~/.obscura/`` (or ``$OBSCURA_HOME``).

    Either may not exist on disk; callers should check ``.is_dir()``
    before reading.  If local == global (no project-local override),
    both elements are identical.
    """
    working_dir = (cwd or Path.cwd()).resolve()
    local_home = working_dir / ".obscura"
    global_home = resolve_obscura_global_home()
    return local_home, global_home


def _merge_order_dirs(
    local_home: Path,
    global_home: Path,
    subdir: str,
) -> list[Path]:
    """Return subdirectories in merge order (global first, local last).

    Deduplicates when local == global.
    """
    dirs: list[Path] = []
    global_sub = global_home / subdir
    local_sub = local_home / subdir
    if global_sub.is_dir() and global_sub != local_sub:
        dirs.append(global_sub)
    if local_sub.is_dir():
        dirs.append(local_sub)
    return dirs


def resolve_all_specs_dirs(cwd: Path | None = None) -> list[Path]:
    """Return specs directories in merge order (global first, local last)."""
    local, global_ = resolve_all_obscura_homes(cwd)
    return _merge_order_dirs(local, global_, "specs")


def resolve_all_mcp_dirs(cwd: Path | None = None) -> list[Path]:
    """Return MCP config directories in merge order."""
    local, global_ = resolve_all_obscura_homes(cwd)
    return _merge_order_dirs(local, global_, "mcp")


def resolve_all_hooks_dirs(cwd: Path | None = None) -> list[Path]:
    """Return hooks directories in merge order."""
    local, global_ = resolve_all_obscura_homes(cwd)
    return _merge_order_dirs(local, global_, "hooks")


def resolve_all_evals_dirs(cwd: Path | None = None) -> list[Path]:
    """Return evals directories in merge order (global first, local last)."""
    local, global_ = resolve_all_obscura_homes(cwd)
    return _merge_order_dirs(local, global_, "evals")


def resolve_all_skills_dirs(cwd: Path | None = None) -> list[Path]:
    """Return skill directories: obscura (global + local) then ~/.claude/skills/.

    Deduplicates and only returns directories that exist on disk.
    """
    dirs: list[Path] = []
    seen: set[Path] = set()

    local, global_ = resolve_all_obscura_homes(cwd)
    for parent in (global_, local):
        skill_dir = parent / "skills"
        resolved = skill_dir.resolve()
        if skill_dir.is_dir() and resolved not in seen:
            dirs.append(skill_dir)
            seen.add(resolved)

    claude_skills = Path.home() / ".claude" / "skills"
    if claude_skills.is_dir() and claude_skills.resolve() not in seen:
        dirs.append(claude_skills)
        seen.add(claude_skills.resolve())

    return dirs


def resolve_obscura_commands_dir(cwd: Path | None = None) -> Path:
    """Resolve directory containing markdown command documents."""
    return resolve_obscura_home(cwd) / "commands"


def resolve_all_agents_dirs(cwd: Path | None = None) -> list[Path]:
    """Return agent definition directories in merge order.

    Order (lowest to highest priority):
      1. Built-in (``obscura/agent/builtin/``)
      2. Global ``~/.obscura/agents/``
      3. Local ``.obscura/agents/``
    """
    dirs: list[Path] = []
    seen: set[Path] = set()

    # Built-in agents (always present).
    if _BUILTIN_DIR.is_dir():
        dirs.append(_BUILTIN_DIR)
        seen.add(_BUILTIN_DIR.resolve())

    local, global_ = resolve_all_obscura_homes(cwd)
    for parent in (global_, local):
        agents_dir = parent / "agents"
        resolved = agents_dir.resolve()
        if agents_dir.is_dir() and resolved not in seen:
            dirs.append(agents_dir)
            seen.add(resolved)

    return dirs


def resolve_all_commands_dirs(cwd: Path | None = None) -> list[Path]:
    """Return command directories: ~/.obscura/commands/ then ~/.claude/commands/.

    Deduplicates and only returns directories that exist on disk.
    """
    dirs: list[Path] = []
    seen: set[Path] = set()

    # Primary: obscura commands (global + local merge)
    local, global_ = resolve_all_obscura_homes(cwd)
    for parent in (global_, local):
        cmd_dir = parent / "commands"
        resolved = cmd_dir.resolve()
        if cmd_dir.is_dir() and resolved not in seen:
            dirs.append(cmd_dir)
            seen.add(resolved)

    # Fallback: ~/.claude/commands/
    claude_cmds = Path.home() / ".claude" / "commands"
    if claude_cmds.is_dir() and claude_cmds.resolve() not in seen:
        dirs.append(claude_cmds)
        seen.add(claude_cmds.resolve())

    return dirs
