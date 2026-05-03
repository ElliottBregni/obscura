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
from typing import Any, cast

from obscura.core._default_commands import DEFAULT_COMMANDS
from obscura.core._default_docs import (
    CAPABILITY_GUIDE,
    CONFIG_REFERENCE,
    PLUGIN_GUIDE,
    POLICY_GUIDE,
    SPEC_GUIDE,
)
from obscura.core._default_evals import DEFAULT_EVALS
from obscura.core._default_skills import DEFAULT_SKILLS
from obscura.core.config_io import dumps_toml, try_load_config
from obscura.plugins.bootstrapper import run_bootstrap
from obscura.plugins.loader import (
    PluginLoader,
    _apply_plugin_filters,  # pyright: ignore[reportPrivateUsage]
)

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
    for subdir in (
        "mcp",
        "hooks",
        "skills",
        "sessions",
        "memory",
        "state",
        "plugins",
        "commands",
        "evals",
        "goals",
    ):
        (ws / subdir).mkdir(parents=True, exist_ok=True)
        logger.info("Created %s/", ws / subdir)

    # -- vault zone structure (always at ~/.obscura/vault/) -----------------
    try:
        from obscura.kairos.vault_sync import VaultSync
        from obscura.vault_provisioner import VaultProvisionError, provision_vault

        vault_dir = _resolve_global_home() / "vault"
        if not vault_dir.exists():
            try:
                provision_vault(
                    "vault",
                    repo_type="vault",
                    destination=_resolve_global_home(),
                )
                logger.info("Vault repo provisioned at %s", vault_dir)
            except VaultProvisionError as exc:
                logger.warning("Vault repo provisioning failed: %s", exc)

        VaultSync(vault_dir=vault_dir).bootstrap()
        logger.info("Vault zones bootstrapped at %s", vault_dir)
    except Exception as exc:
        logger.debug("Vault bootstrap skipped: %s", exc)

    # -- specs/ scaffold -----------------------------------------------------
    for specs_subdir in (
        "specs/templates",
        "specs/policies",
        "specs/workspaces",
        "specs/packs",
    ):
        (ws / specs_subdir).mkdir(parents=True, exist_ok=True)
        logger.info("Created %s/", ws / specs_subdir)

    # -- agents.yaml ---------------------------------------------------------
    _copy_or_create(
        src=global_home / "agents.yaml",
        dst=ws / "agents.yaml",
        default_content=_DEFAULT_AGENTS_YAML,
        force=force,
    )

    # -- agents-available.yaml ------------------------------------------------
    _copy_or_create(
        src=global_home / "agents-available.yaml",
        dst=ws / "agents-available.yaml",
        default_content=_DEFAULT_AGENTS_AVAILABLE_YAML,
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

    # -- hooks/session-init.py -----------------------------------------------
    _copy_or_create(
        src=global_home / "hooks" / "session-init.py",
        dst=ws / "hooks" / "session-init.py",
        default_content=_DEFAULT_SESSION_INIT_PY,
        force=force,
    )
    _make_executable(ws / "hooks" / "session-init.py")

    # -- config.toml --------------------------------------------------------
    _write_if_missing(
        dst=ws / "config.toml",
        content=_DEFAULT_CONFIG_TOML,
        force=force,
    )

    # -- seed specs ----------------------------------------------------------
    _write_if_missing(
        dst=ws / "specs" / "templates" / "base-agent.toml",
        content=_build_default_base_agent_template(),
        force=force,
    )
    _write_if_missing(
        dst=ws / "specs" / "policies" / "safe-dev.toml",
        content=_DEFAULT_SAFE_DEV_POLICY,
        force=force,
    )
    _write_if_missing(
        dst=ws / "specs" / "workspaces" / "default.toml",
        content=_DEFAULT_WORKSPACE,
        force=force,
    )

    # -- docs ----------------------------------------------------------------
    (ws / "docs").mkdir(parents=True, exist_ok=True)
    _write_if_missing(
        dst=ws / "docs" / "PLUGIN_GUIDE.md",
        content=PLUGIN_GUIDE,
        force=force,
    )
    _write_if_missing(
        dst=ws / "docs" / "CONFIG_REFERENCE.md",
        content=CONFIG_REFERENCE,
        force=force,
    )
    _write_if_missing(
        dst=ws / "docs" / "SPEC_GUIDE.md",
        content=SPEC_GUIDE,
        force=force,
    )
    _write_if_missing(
        dst=ws / "docs" / "POLICY_GUIDE.md",
        content=POLICY_GUIDE,
        force=force,
    )
    _write_if_missing(
        dst=ws / "docs" / "CAPABILITY_GUIDE.md",
        content=CAPABILITY_GUIDE,
        force=force,
    )

    # -- default @commands ---------------------------------------------------
    for filename, content in DEFAULT_COMMANDS.items():
        _write_if_missing(
            dst=ws / "commands" / filename,
            content=content,
            force=force,
        )

    # -- default $skills -----------------------------------------------------
    from obscura.core._default_skills import DEFAULT_SKILLS  # noqa: PLC0415

    for skill_name, content in DEFAULT_SKILLS.items():
        skill_dir = ws / "skills" / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        _write_if_missing(
            dst=skill_dir / "SKILL.md",
            content=content,
            force=force,
        )

    # -- default *evals ------------------------------------------------------
    from obscura.core._default_evals import DEFAULT_EVALS  # noqa: PLC0415

    for cmd_name, content in DEFAULT_EVALS.items():
        _write_if_missing(
            dst=ws / "evals" / f"{cmd_name}.eval.md",
            content=content,
            force=force,
        )

    logger.info("Workspace initialised at %s", ws)
    return ws


def load_workspace_config(cwd: Path | None = None) -> dict[str, Any]:
    """Load and merge workspace config from .obscura/config.toml.

    Searches local ``.obscura/config.toml`` first, then falls back to
    ``~/.obscura/config.toml``.  Also supports deprecated ``.yaml`` files.
    Returns the parsed config as a dict.
    """
    from obscura.core.config_io import try_load_config  # noqa: PLC0415

    resolved_cwd = (cwd or Path.cwd()).resolve()
    search_dirs = [
        _resolve_global_home(),
        resolved_cwd / _WORKSPACE_DIR,
    ]

    merged: dict[str, Any] = _DEFAULT_CONFIG_DICT.copy()
    for base in search_dirs:  # global first, then local overrides
        data = try_load_config(
            base / "config.toml",
            base / "config.yaml",
        )
        if data is not None:
            _deep_merge(merged, data)
            logger.debug("Loaded config from %s", base)

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

    from obscura.plugins.loader import _apply_plugin_filters  # pyright: ignore[reportPrivateUsage]

    all_specs = _apply_plugin_filters(list(loader.discover_builtins()))
    for spec in all_specs:
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
        existing = base.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            _deep_merge(cast(dict[str, Any], existing), cast(dict[str, Any], value))
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
                "shell.exec",
                "file.read",
                "file.write",
                "git.ops",
                "web.browse",
                "search.web",
                "security.scan",
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
    # Active agents — move agents from agents-available.yaml to enable them.
    # Agent fields override defaults; nested dicts merge, lists replace.

    defaults:
      provider: copilot
      max_turns: 25
      mcp_servers: auto
      can_delegate: true
      skills:
        lazy_load: true
      capabilities:
        grant: [shell.exec, file.read, file.write, git.ops, search.web]

    agents:
      - name: assistant
        system_prompt: >
          Analyze requests carefully and invoke relevant tools when needed.

      - name: code-architect
        max_turns: 50
        tags: [architecture, design, code-review]
        system_prompt: >
          You are an expert software architect and full-stack developer.
          Workflow: Understand -> Design -> Test -> Implement -> Review -> Document
""")

_DEFAULT_AGENTS_AVAILABLE_YAML = textwrap.dedent("""\
    # Agent catalog — disabled by default.
    # Move agents to agents.yaml to enable them.

    defaults:
      mcp_servers: auto
      skills:
        lazy_load: true
      capabilities:
        grant: [shell.exec, file.read, file.write, git.ops]

    agents: []
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

_DEFAULT_SESSION_INIT_PY = textwrap.dedent("""\
    #!/usr/bin/env python3
    \"\"\"Session init hook — injects agent context into every session.

    Uses load_agent_configs() to merge global + local agents.yaml,
    then outputs a JSON hook response with the available agent names.
    \"\"\"
    import json
    import sys

    try:
        from obscura.tools.swarm import load_agent_configs

        configs = load_agent_configs()
        agent_names = ", ".join(sorted(configs.keys()))
    except Exception:
        agent_names = ""

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": f"Available agents: {agent_names}" if agent_names else "",
        }
    }))
    sys.exit(0)
""")

_DEFAULT_CONFIG_TOML = textwrap.dedent("""\
    # .obscura/config.toml — Project-level Obscura configuration
    # Inherits from ~/.obscura/config.toml, can override settings.

    # mode = "code" loads all registered tools (unrestricted)
    # mode = "ask"  disables tools (conversational only)
    # mode = "plan" enables read-only tools (research + planning)
    # mode = "diff" enables read + git inspection tools
    mode = "code"

    [plugins]
    load_builtins = true

    [plugins.bootstrap]
    auto_install = true
    lenient_builtins = true

    # Capability grants control which tools are available.
    # Plugins with default_grant=true in their manifest are auto-included.
    # Add capabilities here to grant non-default plugins, or use deny to block.
    # Each capability maps to specific tools (e.g. git.ops → git_diff, git_log, etc.)
    [defaults.capabilities]
    grant = [
        "shell.exec",
        "file.read",
        "file.write",
        "git.ops",
        "web.browse",
        "search.web",
        "security.scan",
    ]
    deny = []

    [mcp]
    auto_discover = true
""")


def _build_default_base_agent_template() -> str:
    """Build base-agent template dynamically with all builtin plugin IDs."""
    from obscura.core.config_io import dumps_toml  # noqa: PLC0415

    try:
        from obscura.plugins.builtins import list_builtin_plugin_ids  # noqa: PLC0415

        plugin_ids = list_builtin_plugin_ids()
    except Exception:  # noqa: BLE001
        plugin_ids = ["system-tools", "websearch", "gitleaks"]

    capability_ids = [
        "shell.exec",
        "file.read",
        "file.write",
        "git.ops",
        "web.browse",
        "search.web",
        "security.scan",
    ]

    doc = {
        "apiVersion": "obscura/v1",
        "kind": "Template",
        "metadata": {
            "name": "base-agent",
            "description": "Common defaults inherited by all agent templates.",
        },
        "spec": {
            "provider": "copilot",
            "agent_type": "loop",
            "max_iterations": 25,
            "plugins": plugin_ids,
            "capabilities": capability_ids,
            "instructions": (
                "You are a helpful AI assistant. Analyse requests carefully\n"
                "and invoke relevant tools when needed.\n"
            ),
        },
    }
    return dumps_toml(doc)


_DEFAULT_SAFE_DEV_POLICY = textwrap.dedent("""\
    apiVersion = "obscura/v1"
    kind = "Policy"

    [metadata]
    name = "safe-dev"
    description = "Conservative policy for development workflows."

    [spec]
    tool_denylist = ["dangerous_tool"]
    require_confirmation = ["bash", "write_file", "delete_file"]
    max_turns = 15
""")

_DEFAULT_WORKSPACE = textwrap.dedent("""\
    apiVersion = "obscura/v1"
    kind = "Workspace"

    [metadata]
    name = "default"
    description = "Default workspace — all plugins, no restrictions."

    [[spec.agents]]
    name = "assistant"
    template = "base-agent"
    mode = "task"

    [spec.startup]
    preload_plugins = true
""")
