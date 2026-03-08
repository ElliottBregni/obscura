"""obscura.core.workspace — Workspace init for local ``.obscura/`` directories.

Creates the project-local ``.obscura/`` scaffold with default config files,
copying from the global ``~/.obscura/`` where available.

Usage::

    from obscura.core.workspace import init_workspace, ensure_workspace

    # Explicit init (raises if already exists)
    ws = init_workspace()

    # Lazy init (creates only if missing)
    ws = ensure_workspace()
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import textwrap
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_WORKSPACE_DIR = ".obscura"


class WorkspaceExistsError(FileExistsError):
    """Raised when ``init_workspace`` is called but ``.obscura/`` already exists."""


def init_workspace(
    cwd: Path | None = None,
    *,
    force: bool = False,
) -> Path:
    """Initialise a local ``.obscura/`` workspace directory.

    Parameters
    ----------
    cwd:
        The directory in which to create ``.obscura/``.  Defaults to
        :func:`Path.cwd`.
    force:
        If *True*, re-create the scaffold even when ``.obscura/`` already
        exists (files that already exist are **not** overwritten unless
        *force* is set).

    Returns
    -------
    Path
        Absolute path to the created ``.obscura/`` directory.

    Raises
    ------
    WorkspaceExistsError
        If ``.obscura/`` already exists and *force* is False.

    """
    resolved_cwd = (cwd or Path.cwd()).resolve()
    ws = resolved_cwd / _WORKSPACE_DIR

    if ws.exists() and not force:
        msg = f"Workspace already exists at {ws}. Pass force=True to reinitialise."
        raise WorkspaceExistsError(msg)

    global_home = _resolve_global_home()

    # -- directories ---------------------------------------------------------
    for subdir in ("mcp", "hooks", "skills", "sessions", "memory"):
        (ws / subdir).mkdir(parents=True, exist_ok=True)
        logger.info("Created %s/", ws / subdir)

    # -- agents.yaml ---------------------------------------------------------
    _copy_or_create(
        src=global_home / "agents.yaml",
        dst=ws / "agents.yaml",
        default_content=_DEFAULT_AGENTS_YAML,
        force=force,
    )

    # -- mcp/mcp.json --------------------------------------------------------
    _write_if_missing(
        dst=ws / "mcp" / "mcp.json",
        content=_DEFAULT_MCP_JSON,
        force=force,
    )

    # -- hooks/hooks.json ----------------------------------------------------
    _write_if_missing(
        dst=ws / "hooks" / "hooks.json",
        content=_DEFAULT_HOOKS_JSON,
        force=force,
    )

    # -- hooks/session-init.sh -----------------------------------------------
    _copy_or_create(
        src=global_home / "hooks" / "session-init.sh",
        dst=ws / "hooks" / "session-init.sh",
        default_content=_DEFAULT_SESSION_INIT_SH,
        force=force,
    )
    _make_executable(ws / "hooks" / "session-init.sh")

    # -- config.yaml --------------------------------------------------------
    _write_if_missing(
        dst=ws / "config.yaml",
        content=_DEFAULT_CONFIG_YAML,
        force=force,
    )

    logger.info("Workspace initialised at %s", ws)
    return ws


def load_workspace_config(cwd: Path | None = None) -> dict[str, Any]:
    """Load and merge workspace config from .obscura/config.yaml.

    Searches local ``.obscura/config.yaml`` first, then falls back to
    ``~/.obscura/config.yaml``.  Returns the parsed YAML as a dict.
    """
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        logger.debug("PyYAML not available — returning default config")
        return _DEFAULT_CONFIG_DICT.copy()

    resolved_cwd = (cwd or Path.cwd()).resolve()
    candidates = [
        resolved_cwd / _WORKSPACE_DIR / "config.yaml",
        _resolve_global_home() / "config.yaml",
    ]

    merged: dict[str, Any] = _DEFAULT_CONFIG_DICT.copy()
    for path in reversed(candidates):  # global first, then local overrides
        if path.is_file():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _deep_merge(merged, data)
                    logger.debug("Loaded config from %s", path)
            except Exception as exc:
                logger.warning("Failed to parse %s: %s", path, exc)

    return merged


def bootstrap_all_builtins(cwd: Path | None = None) -> dict[str, Any]:
    """Run bootstrap for all builtin plugins.

    Reads ``plugins.bootstrap.*`` from the workspace config.yaml and installs
    declared dependencies for each builtin plugin manifest.

    Returns
    -------
    dict
        Keys: ``installed``, ``skipped``, ``errors``, ``warnings`` — each a
        list of ``"plugin_id: dep"`` strings.
    """
    from obscura.plugins.bootstrapper import run_bootstrap
    from obscura.plugins.loader import PluginLoader

    config = load_workspace_config(cwd)
    plugins_cfg = config.get("plugins", {})
    bootstrap_cfg = plugins_cfg.get("bootstrap", {})

    # Respect config.yaml settings
    if not plugins_cfg.get("load_builtins", True):
        logger.info("Builtin plugins disabled in config.yaml")
        return {"installed": [], "skipped": [], "errors": [], "warnings": []}

    if not bootstrap_cfg.get("auto_install", True):
        logger.info("Auto-install disabled in config.yaml")
        return {"installed": [], "skipped": [], "errors": [], "warnings": []}

    lenient = bootstrap_cfg.get("lenient_builtins", True)

    loader = PluginLoader()
    summary: dict[str, Any] = {
        "installed": [],
        "skipped": [],
        "errors": [],
        "warnings": [],
    }

    for spec in loader.discover_builtins():
        if spec.bootstrap is None or not spec.bootstrap.deps:
            continue
        result = run_bootstrap(spec)
        for item in result.installed:
            summary["installed"].append(f"{spec.id}: {item}")
        for item in result.skipped:
            summary["skipped"].append(f"{spec.id}: {item}")
        for item in result.errors:
            if lenient:
                summary["warnings"].append(f"{spec.id}: {item} (lenient)")
            else:
                summary["errors"].append(f"{spec.id}: {item}")
        for item in result.warnings:
            summary["warnings"].append(f"{spec.id}: {item}")

    return summary


def ensure_workspace(cwd: Path | None = None) -> Path:
    """Return the local ``.obscura/`` path, creating it if absent.

    Unlike :func:`init_workspace` this never raises when the directory
    already exists.

    Returns
    -------
    Path
        Absolute path to the ``.obscura/`` directory.

    """
    resolved_cwd = (cwd or Path.cwd()).resolve()
    ws = resolved_cwd / _WORKSPACE_DIR

    if ws.exists():
        logger.debug("Workspace already exists at %s", ws)
        return ws

    return init_workspace(resolved_cwd)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """Recursively merge *override* into *base* (mutates *base*)."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _resolve_global_home() -> Path:
    """Return the global ``~/.obscura`` directory (respects ``OBSCURA_HOME``)."""
    env_home = os.environ.get("OBSCURA_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".obscura").resolve()


def _copy_or_create(
    *,
    src: Path,
    dst: Path,
    default_content: str,
    force: bool,
) -> None:
    """Copy *src* to *dst* if *src* exists, otherwise write *default_content*."""
    if dst.exists() and not force:
        logger.debug("Skipping existing file %s", dst)
        return

    if src.is_file():
        shutil.copy2(src, dst)
        logger.info("Copied %s -> %s", src, dst)
    else:
        dst.write_text(default_content, encoding="utf-8")
        logger.info("Created default %s", dst)


def _write_if_missing(*, dst: Path, content: str, force: bool) -> None:
    """Write *content* to *dst* only when it does not yet exist (or *force*)."""
    if dst.exists() and not force:
        logger.debug("Skipping existing file %s", dst)
        return

    dst.write_text(content, encoding="utf-8")
    logger.info("Created %s", dst)


def _make_executable(path: Path) -> None:
    """Add the executable bit for the file owner."""
    current = path.stat().st_mode
    path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# Default config dict (used when config.yaml is missing or unreadable)
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_DICT: dict[str, Any] = {
    "plugins": {
        "load_builtins": True,
        "bootstrap": {
            "auto_install": True,
            "lenient_builtins": True,
        },
    },
    "mode": "code",
    "defaults": {
        "capabilities": {
            "grant": [
                "shell.exec", "file.read", "file.write", "git.ops",
                "web.browse", "search.web", "security.scan",
            ],
            "deny": [],
        },
    },
    "mcp": {
        "auto_discover": True,
    },
}


# ---------------------------------------------------------------------------
# Default file contents
# ---------------------------------------------------------------------------

_DEFAULT_AGENTS_YAML = textwrap.dedent("""\
    agents:
      - name: assistant
        type: loop
        model: copilot
        system_prompt: >-
          Analyze requests carefully and invoke relevant skills when needed.
        max_turns: 25
        mcp_servers: auto
        skills:
          lazy_load: true
          filter: null

      - name: code-architect
        type: loop
        model: copilot
        system_prompt: |
          You are an expert software architect and full-stack developer.
          Workflow: Understand → Design → Test → Implement → Review → Document
        max_turns: 50
        mcp_servers: auto
        skills:
          lazy_load: true
          filter: null
""")

_DEFAULT_MCP_JSON = json.dumps({"mcpServers": {}}, indent=2) + "\n"

_DEFAULT_HOOKS_JSON = (
    json.dumps(
        {
            "hooks": {
                "preToolUse": [],
                "postToolUse": [],
                "onSessionStart": [],
                "onSessionEnd": [],
            },
        },
        indent=2,
    )
    + "\n"
)

_DEFAULT_SESSION_INIT_SH = textwrap.dedent("""\
    #!/usr/bin/env bash
    # Session init hook — injects agent context into every session

    AGENTS_YAML="${OBSCURA_HOME:-$HOME/.obscura}/agents.yaml"

    AGENT_NAMES=$(grep -E '^\\s+- name:' "$AGENTS_YAML" 2>/dev/null \\
      | awk '{print $NF}' | tr '\\n' ', ' | sed 's/,$//')

    cat << EOF
    {
      "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "Available agents: ${AGENT_NAMES}"
      }
    }
    EOF

    exit 0
""")

_DEFAULT_CONFIG_YAML = textwrap.dedent("""\
    # .obscura/config.yaml — Project-level Obscura configuration
    # Inherits from ~/.obscura/config.yaml, can override settings.

    # ── Plugin Loading ─────────────────────────────────────────────────────
    plugins:
      load_builtins: true

      bootstrap:
        auto_install: true
        lenient_builtins: true

    # ── Default Mode ──────────────────────────────────────────────────────
    # mode=code loads all registered tools (unrestricted)
    # mode=ask  disables tools (conversational only)
    # mode=plan enables read-only tools (research + planning)
    # mode=diff enables read + git inspection tools
    mode: code

    # ── Default Capabilities ──────────────────────────────────────────────
    defaults:
      capabilities:
        grant:
          - shell.exec
          - file.read
          - file.write
          - git.ops
          - web.browse
          - search.web
          - security.scan
        deny: []

    # ── MCP Servers ───────────────────────────────────────────────────────
    mcp:
      auto_discover: true
""")
