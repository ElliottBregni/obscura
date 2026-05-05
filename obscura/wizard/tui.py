"""Terminal wizard built on prompt_toolkit dialogs + Rich for layout.

This is a thin frontend over :class:`obscura.wizard.service.WizardService`.
Anything beyond user input/output should live in the service.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from prompt_toolkit.shortcuts import (
    button_dialog,
    checkboxlist_dialog,
    input_dialog,
    message_dialog,
    radiolist_dialog,
    yes_no_dialog,
)
from rich.console import Console
from rich.table import Table

from obscura.wizard.schema import Profile
from obscura.wizard.service import WizardService

logger = logging.getLogger(__name__)


def run(service: WizardService | None = None) -> int:
    """Launch the interactive wizard. Returns a process exit code."""
    svc = service or WizardService()
    console = Console()

    while True:
        snapshot = svc.snapshot()
        _render_summary(console, snapshot)

        choice = button_dialog(
            title="Obscura Wizard",
            text=(
                f"Active profile: {snapshot.active.profile}\n"
                f"Profiles: {len(snapshot.profiles)}    "
                f"Workspaces bound: {len(snapshot.workspaces)}"
            ),
            buttons=[
                ("Profile", "edit_profile"),
                ("Active", "set_active"),
                ("Workspace", "bind_workspace"),
                ("Env", "edit_env"),
                ("Soul", "edit_soul"),
                ("Quit", "quit"),
            ],
        ).run()

        if choice in (None, "quit"):
            return 0
        if choice == "edit_profile":
            _edit_profile_flow(svc, snapshot)
        elif choice == "set_active":
            _set_active_flow(svc, snapshot)
        elif choice == "bind_workspace":
            _bind_workspace_flow(svc, snapshot)
        elif choice == "edit_env":
            _edit_env_flow(svc, snapshot)
        elif choice == "edit_soul":
            _edit_soul_flow(svc, snapshot)


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


def _render_summary(console: Console, snapshot) -> None:
    console.rule("[bold cyan]Obscura Wizard[/bold cyan]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Profile")
    table.add_column("Backend")
    table.add_column("Prompts")
    table.add_column("Capabilities")
    for p in snapshot.profiles:
        marker = "[green]●[/green] " if p.name == snapshot.active.profile else "  "
        table.add_row(
            f"{marker}{p.name}",
            p.backend or "-",
            ", ".join(p.prompts) or "-",
            f"{len(p.capabilities)} grants",
        )
    if not snapshot.profiles:
        table.add_row("(none yet)", "-", "-", "-")
    console.print(table)


# ----------------------------------------------------------------------
# Flows
# ----------------------------------------------------------------------


def _edit_profile_flow(svc: WizardService, snapshot) -> None:
    options = [(p.name, p.name) for p in snapshot.profiles]
    options.append(("__new__", "+ New profile"))
    target = radiolist_dialog(
        title="Edit profile",
        text="Choose a profile to edit, or create a new one.",
        values=options,
    ).run()
    if target is None:
        return

    if target == "__new__":
        name = input_dialog(
            title="New profile",
            text="Profile name (e.g. 'research', 'coding'):",
        ).run()
        if not name:
            return
        existing = Profile(name=name)
    else:
        existing = svc.get_profile(target) or Profile(name=target)

    backend_choices = [(b, b) for b in snapshot.available_backends]
    backend_choices.insert(0, ("", "(inherit)"))
    backend = radiolist_dialog(
        title=f"Backend for {existing.name}",
        text="Select default backend.",
        values=backend_choices,
        default=existing.backend or "",
    ).run()
    if backend is None:
        return  # user cancelled

    mode_choices = [(m, m) for m in snapshot.available_modes]
    mode_choices.insert(0, ("", "(inherit global mode)"))
    mode = radiolist_dialog(
        title=f"Tool-loading mode for {existing.name}",
        text=(
            "code = all tools, ask = none, plan = read-only, "
            "diff = read + git inspection."
        ),
        values=mode_choices,
        default=existing.mode or "",
    ).run()
    if mode is None:
        return

    vault_path = input_dialog(
        title=f"Vault path for {existing.name}",
        text=(
            "Optional override for ~/.obscura/vault. Leave blank to inherit. "
            f"Default: {snapshot.default_vault_path}"
        ),
        default=existing.vault_path or "",
    ).run()
    if vault_path is None:
        return

    prompts = checkboxlist_dialog(
        title=f"Prompts for {existing.name}",
        text="Select prompt files to compose into the system prompt.",
        values=[(p, p) for p in snapshot.available_prompts],
        default_values=[p for p in existing.prompts if p in snapshot.available_prompts],
    ).run()
    if prompts is None:
        return

    capabilities = checkboxlist_dialog(
        title=f"Capabilities for {existing.name}",
        text="Select capability grants.",
        values=[(c, c) for c in snapshot.available_capabilities],
        default_values=[
            c for c in existing.capabilities if c in snapshot.available_capabilities
        ],
    ).run()
    if capabilities is None:
        return

    plugins = checkboxlist_dialog(
        title=f"Plugins for {existing.name}",
        text="Select plugins to load with this profile.",
        values=[(p, p) for p in snapshot.available_plugins] or [("", "(none found)")],
        default_values=[p for p in existing.plugins if p in snapshot.available_plugins],
    ).run()
    if plugins is None:
        return

    mcp_servers = checkboxlist_dialog(
        title=f"MCP servers for {existing.name}",
        text="Select MCP servers to attach.",
        values=[(m, m) for m in snapshot.available_mcp_servers]
        or [("", "(none found)")],
        default_values=[
            m for m in existing.mcp_servers if m in snapshot.available_mcp_servers
        ],
    ).run()
    if mcp_servers is None:
        return

    agents = checkboxlist_dialog(
        title=f"Agents for {existing.name}",
        text="Select agents to make available.",
        values=[(a, a) for a in snapshot.available_agents] or [("", "(none found)")],
        default_values=[a for a in existing.agents if a in snapshot.available_agents],
    ).run()
    if agents is None:
        return

    skills = checkboxlist_dialog(
        title=f"Skills for {existing.name}",
        text="Select skills to load. Leave empty to load all discovered skills.",
        values=[(s, s) for s in snapshot.available_skills] or [("", "(none found)")],
        default_values=[s for s in existing.skills if s in snapshot.available_skills],
    ).run()
    if skills is None:
        return

    profile = Profile(
        name=existing.name,
        prompts=[p for p in prompts if p],
        backend=backend or None,
        model=existing.model,
        mode=mode or None,
        capabilities=[c for c in capabilities if c],
        plugins=[p for p in plugins if p],
        mcp_servers=[m for m in mcp_servers if m],
        agents=[a for a in agents if a],
        skills=[s for s in skills if s],
        vault_path=vault_path.strip() or None,
    )
    svc.upsert_profile(profile)
    message_dialog(title="Saved", text=f"Profile '{profile.name}' saved.").run()


def _set_active_flow(svc: WizardService, snapshot) -> None:
    if not snapshot.profiles:
        message_dialog(title="No profiles", text="Create a profile first.").run()
        return
    target = radiolist_dialog(
        title="Set active profile",
        text="This becomes the default profile when no workspace override matches.",
        values=[(p.name, p.name) for p in snapshot.profiles],
        default=snapshot.active.profile,
    ).run()
    if target:
        svc.set_active(target)
        message_dialog(
            title="Active set", text=f"Active profile is now '{target}'."
        ).run()


def _bind_workspace_flow(svc: WizardService, snapshot) -> None:
    if not snapshot.profiles:
        message_dialog(title="No profiles", text="Create a profile first.").run()
        return
    cwd = str(Path.cwd())
    path = input_dialog(
        title="Workspace path",
        text="Absolute path to bind (defaults to current working directory):",
        default=cwd,
    ).run()
    if not path:
        return
    profile = radiolist_dialog(
        title="Profile for workspace",
        text=f"Which profile should activate when cwd={path}?",
        values=[(p.name, p.name) for p in snapshot.profiles],
    ).run()
    if not profile:
        return
    svc.set_workspace(path, profile)
    if yes_no_dialog(
        title="Bound",
        text=f"Workspace '{path}' is now bound to '{profile}'. Continue?",
    ).run():
        return


def _edit_env_flow(svc: WizardService, snapshot) -> None:
    """Read or write the per-profile env file (~/.obscura/.env.<profile>).

    Spawns ``$EDITOR`` (falling back to ``$VISUAL`` then ``vi``) so the user
    can edit a real file with their normal keybindings — multi-line input
    in a prompt_toolkit dialog feels cramped for anything more than a few
    lines.
    """
    if not snapshot.profiles:
        message_dialog(title="No profiles", text="Create a profile first.").run()
        return
    target = radiolist_dialog(
        title="Per-profile env file",
        text="Select the profile whose .env you want to edit.",
        values=[(p.name, p.name) for p in snapshot.profiles],
        default=snapshot.active.profile,
    ).run()
    if not target:
        return

    current = svc.read_env_file(target)
    seed = current or (
        "# KEY=value pairs, one per line.\n"
        "# Loaded after ~/.obscura/.env when this profile is active.\n"
        "# File is written 0600 (owner read/write only).\n"
    )
    edited = _edit_in_external_editor(seed, suffix=f".env.{target}")
    if edited is None:
        message_dialog(
            title="Cancelled", text="No editor available; nothing saved."
        ).run()
        return
    if edited == current:
        message_dialog(title="No changes", text="File unchanged.").run()
        return
    path = svc.write_env_file(target, edited)
    message_dialog(title="Saved", text=f"Wrote {path}").run()


def _edit_soul_flow(svc: WizardService, snapshot) -> None:
    """Edit ``~/.obscura/SOUL.md`` in ``$EDITOR``."""
    current = svc.read_soul()
    seed = current or (
        "# SOUL\n\n"
        "Your personalized soul/personality file. Loaded by the agent at "
        "startup. Plain markdown — describe who you are, how you like to "
        "work, what to remember.\n"
    )
    edited = _edit_in_external_editor(seed, suffix=".SOUL.md")
    if edited is None:
        message_dialog(
            title="Cancelled",
            text="No editor available; nothing saved.",
        ).run()
        return
    if edited == current:
        message_dialog(title="No changes", text="SOUL.md unchanged.").run()
        return
    path = svc.write_soul(edited)
    message_dialog(title="Saved", text=f"Wrote {path}").run()


def _edit_in_external_editor(seed: str, *, suffix: str) -> str | None:
    """Open ``$EDITOR`` on a tempfile pre-filled with ``seed``; return the result.

    Returns ``None`` if no editor can be located. Honours ``$EDITOR``,
    then ``$VISUAL``, then ``vi``. ``$EDITOR`` may include arguments
    (e.g. ``code --wait``) — parsed via :func:`shlex.split`.
    """
    editor_cmd = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    try:
        argv = shlex.split(editor_cmd)
    except ValueError:
        logger.debug("could not parse EDITOR=%s", editor_cmd, exc_info=True)
        return None
    if not argv:
        return None

    # NamedTemporaryFile + manual close so Windows can re-open the path.
    fd, tmp_name = tempfile.mkstemp(suffix=suffix, prefix="obscura-wizard-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(seed)
        try:
            subprocess.run([*argv, tmp_name], check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            logger.debug(
                "editor invocation failed: %s %s", argv, tmp_name, exc_info=True
            )
            return None
        return Path(tmp_name).read_text(encoding="utf-8")
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            logger.debug("could not unlink %s", tmp_name, exc_info=True)
