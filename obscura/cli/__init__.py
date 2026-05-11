"""obscura.cli — Claude Code-style REPL for Obscura.

Single entry point: ``obscura`` drops into an interactive REPL.
Slash commands (``/help``, ``/agent``, ``/session``, etc.) for actions;
everything else is a chat message sent to the backend.

Usage::

    # Interactive REPL (default)
    obscura
    obscura -b claude
    obscura -b codex

    # Single-shot
    obscura "explain this code"
    obscura -b claude -m claude-sonnet-4-5-20250929 "summarize"

Implementation note
-------------------
The bulk of the logic lives in focused sub-modules that can be imported and
tested independently:

  _env_loader.py   -- .env / secrets materialisation
  _guide_sync.py   -- OBSCURA.md <-> CLAUDE.md sync + provider settings
  _tool_confirm.py -- TUI tool-confirm, file-change tracking, plan parsing
  _daemon.py       -- iMessage daemon lifecycle
  _send.py         -- send_message (streaming + retry)
  _repl_loop.py    -- repl() async loop (session bootstrap + input loop)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Literal, cast

import click

from obscura.core.paths import resolve_obscura_home, resolve_obscura_specs_dir
from obscura.core.enums.agent import Backend

# ---------------------------------------------------------------------------
# Sub-module public re-exports (keep for backwards compat + single import point)
# `as` aliases mark these explicit re-exports per PEP 484.
# ---------------------------------------------------------------------------
from obscura.cli._daemon import start_imessage_daemon as start_imessage_daemon
from obscura.cli._env_loader import (
    bootstrap_env as bootstrap_env,
    load_dotenvs as load_dotenvs,
    materialize_secrets as materialize_secrets,
)
from obscura.cli._guide_sync import (
    sync_guide_files as sync_guide_files,
    sync_provider_settings as sync_provider_settings,
)
from obscura.cli._repl_loop import repl as repl
from obscura.cli._send import send_message as send_message
from obscura.cli._tool_confirm import (
    cli_confirm as cli_confirm,
    maybe_parse_plan as maybe_parse_plan,
    track_file_event as track_file_event,
)
from obscura.cli.bootstrap import _discover_mcp as _discover_mcp  # pyright: ignore[reportPrivateUsage]

# ---------------------------------------------------------------------------
# Internal aliases kept for callers that used the private names
# ---------------------------------------------------------------------------
_sync_guide_files = sync_guide_files
_sync_provider_settings = sync_provider_settings
_cli_confirm = cli_confirm
_track_file_event = track_file_event
_maybe_parse_plan = maybe_parse_plan
_start_imessage_daemon = start_imessage_daemon
_repl = repl

_log = logging.getLogger("obscura.cli")


def _is_interactive_repl(prompt: str | None) -> bool:
    """Return True when running the interactive REPL (not single-shot)."""
    return prompt is None


def _ensure_cli_auth_for_startup(
    backend: str,
    prompt: str | None,
) -> None:
    """Auto-run CLI GitHub OAuth for interactive Copilot sessions when needed."""
    if backend != "copilot" or not _is_interactive_repl(prompt):
        return

    try:
        from obscura.cli.auth_commands import ensure_github_oauth_session

        ensure_github_oauth_session(open_browser=True)
    except Exception as exc:
        _log.debug("CLI auto-auth skipped: %s", exc)


# ---------------------------------------------------------------------------
# Click entry point
# ---------------------------------------------------------------------------


class _SubcommandAwareGroup(click.Group):
    """Click group that prefers subcommand dispatch over positional capture.

    The root group has both ``invoke_without_command=True`` and a
    positional ``prompt`` argument so ``obscura "explain this"`` works
    as a single-shot. But Click's default parsing then greedily eats
    the next positional token — including subcommand names like
    ``init``, ``kairos``, ``tui`` — so ``obscura tui`` would launch
    the REPL with prompt="tui" instead of dispatching the ``tui``
    subcommand.

    This subclass intercepts ``parse_args`` and, when the first
    non-option token matches a registered subcommand, drops the
    positional ``prompt`` argument so Click routes to the subcommand
    cleanly.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:  # pyright: ignore[reportImplicitOverride]
        for tok in args:
            if tok.startswith("-"):
                continue
            if tok in self.commands:
                # Pop the prompt argument so it doesn't consume `tok`.
                self.params = [
                    p
                    for p in self.params
                    if not (isinstance(p, click.Argument) and p.name == "prompt")
                ]
            break
        return super().parse_args(ctx, args)


@click.group(cls=_SubcommandAwareGroup, invoke_without_command=True)
@click.argument("prompt", required=False, default=None)
@click.option(
    "-b",
    "--backend",
    default="copilot",
    type=click.Choice([b.value for b in Backend]),
    help="Backend to use.",
)
@click.option("-m", "--model", default=None, help="Model ID override.")
@click.option("-s", "--system", default="", help="System prompt.")
@click.option("--session", default=None, help="Resume session by ID.")
@click.option(
    "--continue",
    "resume_last",
    is_flag=True,
    default=False,
    help="Resume the most recent session.",
)
@click.option(
    "--resume",
    default=None,
    help="Resume session by ID (alias for --session).",
)
@click.option("--max-turns", default=10, type=int, help="Max agent loop turns.")
@click.option(
    "--tools",
    default="on",
    type=click.Choice(["on", "off"]),
    help="Enable/disable tool calling.",
)
@click.option(
    "--confirm/--no-confirm",
    default=False,
    help="Require approval before each tool call.",
)
@click.option(
    "--no-default-prompt",
    is_flag=True,
    default=False,
    help="Skip the default Obscura system prompt.",
)
@click.option(
    "-w",
    "--workspace",
    "workspace_name",
    default=None,
    help="Load a workspace from .obscura/specs/ (e.g. 'code-mode').",
)
@click.option(
    "--log-level",
    "log_level",
    default="WARNING",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Console log level.",
)
@click.option(
    "--supervise/--no-supervise",
    default=True,
    help="Launch the agent fleet from agents.yaml (default: on).",
)
@click.option(
    "--debug-tui",
    "debug_tui",
    is_flag=True,
    default=False,
    help="Start the TUI in debug display mode (raw payloads + traces).",
)
@click.pass_context
def main(
    ctx: click.Context,
    prompt: str | None = None,
    backend: str = "copilot",
    model: str | None = None,
    system: str = "",
    session: str | None = None,
    resume_last: bool = False,
    resume: str | None = None,
    max_turns: int = 10,
    tools: str = "on",
    confirm: bool = False,
    no_default_prompt: bool = False,
    workspace_name: str | None = None,
    log_level: str = "WARNING",
    supervise: bool = True,
    debug_tui: bool = False,
) -> None:
    """Obscura -- AI agent REPL."""
    # If a subcommand was invoked, let Click handle it
    if ctx.invoked_subcommand is not None:
        return

    import logging as _logging

    cli_logger = _logging.getLogger("obscura")
    level = getattr(_logging, log_level.upper(), _logging.WARNING)
    for h in cli_logger.handlers:
        if h.__class__.__name__ == "InfoHandler":
            h.setLevel(level)

    # Sync OBSCURA.md <-> CLAUDE.md before anything else touches the workspace.
    sync_guide_files()

    # Disable provider permission layers.
    sync_provider_settings()

    # Detect and optionally import external agent configs.
    try:
        from obscura.core.migrate_external import run_startup_migration

        run_startup_migration(interactive=prompt is None)
    except Exception as exc:
        _log.debug("External migration check failed: %s", exc)

    # Compile workspace if specified
    compiled_ws = None
    if workspace_name is not None:
        try:
            from obscura.core.compiler.compile import compile_workspace

            compiled_ws = compile_workspace(workspace_name, strict=False)
            ws_backend = compiled_ws.config.get("default_backend")
            if ws_backend and isinstance(ws_backend, str):
                backend = ws_backend
            click.echo(
                f"Loaded workspace '{compiled_ws.name}' "
                f"({len(compiled_ws.agents)} agents, "
                f"{len(compiled_ws.policies)} policies)",
            )
        except Exception as exc:
            _log.debug("suppressed exception in main", exc_info=True)
            click.echo(f"Failed to load workspace '{workspace_name}': {exc}", err=True)

    _ensure_cli_auth_for_startup(backend, prompt)

    # Resolve session ID: --resume > --session > --continue (last session)
    resolved_session = resume or session
    if not resolved_session and resume_last:
        try:
            import sqlite3

            db_path = resolve_obscura_home() / "events.db"
            con = sqlite3.connect(str(db_path))
            row = con.execute(
                "SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1",
            ).fetchone()
            con.close()
            if row:
                resolved_session = row[0]
        except Exception:
            _log.debug("suppressed exception in main", exc_info=True)

    # --debug-tui beats env, env beats default.
    tui_mode = (
        "debug"
        if (debug_tui or os.environ.get("OBSCURA_TUI_DEBUG", "").strip().lower() == "1")
        else "normal"
    )

    try:
        asyncio.run(
            repl(
                backend,
                model,
                system,
                resolved_session,
                max_turns,
                tools,
                prompt,
                confirm,
                no_default_prompt,
                supervise=supervise,
                compiled_ws=compiled_ws,
                tui_display_mode=tui_mode,
            ),
        )
    except KeyboardInterrupt:
        _log.debug("suppressed exception in main", exc_info=True)


@main.command()
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Reinitialise even if .obscura/ exists.",
)
@click.option(
    "--no-bootstrap",
    is_flag=True,
    default=False,
    help="Skip plugin dependency bootstrapping.",
)
def init(force: bool, no_bootstrap: bool) -> None:
    """Initialise a local .obscura/ workspace and bootstrap plugin deps."""
    from obscura.core.workspace import (
        WorkspaceExistsError,
        bootstrap_all_builtins,
        init_workspace,
    )

    sync_guide_files()
    sync_provider_settings()

    try:
        ws = init_workspace(force=force)
        click.echo(f"Workspace initialised at {ws}")
    except WorkspaceExistsError:
        _log.debug("suppressed exception in init", exc_info=True)
        click.echo(".obscura/ already exists. Use --force to reinitialise.")
        if no_bootstrap:
            return
    except Exception as exc:
        _log.debug("suppressed exception in init", exc_info=True)
        click.echo(f"Init failed: {exc}", err=True)
        return

    if not no_bootstrap:
        click.echo("Bootstrapping plugin dependencies...")
        try:
            summary = bootstrap_all_builtins()
            if summary["installed"]:
                click.echo(f"  Installed: {', '.join(summary['installed'])}")
            if summary["skipped"]:
                click.echo(f"  Already present: {len(summary['skipped'])} deps")
            if summary["errors"]:
                click.echo(f"  Failed: {', '.join(summary['errors'])}", err=True)
            if summary["warnings"]:
                for w in summary["warnings"]:
                    click.echo(f"  Warning: {w}", err=True)
            if not summary["errors"]:
                click.echo("All plugin dependencies bootstrapped.")
            else:
                click.echo(
                    "Some deps failed. Install manually: "
                    + ", ".join(e.split(":")[0] for e in summary["errors"]),
                )
        except Exception as exc:
            _log.debug("suppressed exception in init", exc_info=True)
            click.echo(f"Bootstrap failed: {exc}", err=True)


# ---------------------------------------------------------------------------
# Workspace subcommands
# ---------------------------------------------------------------------------


@main.group()
def workspace() -> None:
    """Manage workspaces (list, inspect, compile)."""


@workspace.command("list")
def workspace_list() -> None:
    """List available workspaces from specs directory."""
    from obscura.core.compiler.loader import load_specs_dir

    specs_dir = resolve_obscura_specs_dir()
    if not specs_dir.is_dir():
        click.echo(f"No specs directory at {specs_dir}")
        return

    registry = load_specs_dir(specs_dir)
    if not registry.workspaces:
        click.echo("No workspaces found.")
        return

    for name, ws in sorted(registry.workspaces.items()):
        desc = ws.metadata.description or "(no description)"
        n_agents = len(ws.spec.agents)
        n_policies = len(ws.spec.policies)
        click.echo(f"  {name:20s}  {n_agents} agents, {n_policies} policies  {desc}")


@workspace.command("inspect")
@click.argument("name")
def workspace_inspect(name: str) -> None:
    """Compile and inspect a workspace."""
    from obscura.core.compiler.compile import compile_workspace_from_dir
    from obscura.core.compiler.errors import CompileError

    specs_dir = resolve_obscura_specs_dir()
    try:
        ws = compile_workspace_from_dir(name, specs_dir, strict=False)
    except CompileError as exc:
        _log.debug("suppressed exception in workspace_inspect", exc_info=True)
        click.echo(f"Compile error: {exc}", err=True)
        return

    click.echo(f"Workspace: {ws.name}")
    click.echo(f"  Config: {ws.config or '(empty)'}")
    click.echo(f"  Preload plugins: {ws.preload_plugins}")

    if ws.policies:
        click.echo(f"  Policies: {', '.join(p.name for p in ws.policies)}")
    if ws.plugin_include:
        click.echo(f"  Plugin include: {', '.join(sorted(ws.plugin_include))}")
    if ws.plugin_exclude:
        click.echo(f"  Plugin exclude: {', '.join(sorted(ws.plugin_exclude))}")
    if ws.memory:
        click.echo(
            f"  Memory: namespace={ws.memory.namespace} scope={ws.memory.shared_scope}",
        )

    if ws.agents:
        click.echo(f"  Agents ({len(ws.agents)}):")
        for a in ws.agents:
            click.echo(
                f"    {a.name:20s}  template={a.template_name}  "
                f"mode={a.mode}  provider={a.provider}  "
                f"plugins=[{', '.join(a.plugins)}]",
            )

    if ws.startup_agents:
        click.echo(f"  Startup: {', '.join(ws.startup_agents)}")


@workspace.command("load")
@click.argument("name")
def workspace_load(name: str) -> None:
    """Compile a workspace and display its configuration for the session."""
    from obscura.core.compiler.compile import compile_workspace
    from obscura.core.compiler.errors import CompileError

    try:
        ws = compile_workspace(name, strict=False)
    except CompileError as exc:
        _log.debug("suppressed exception in workspace_load", exc_info=True)
        click.echo(f"Compile error: {exc}", err=True)
        return

    click.echo(f"Loaded workspace: {ws.name}")
    if ws.agents:
        click.echo(f"  Agents: {', '.join(a.name for a in ws.agents)}")
    if ws.policies:
        click.echo(f"  Policies: {', '.join(p.name for p in ws.policies)}")
    if ws.plugin_include:
        click.echo(f"  Allowed plugins: {', '.join(sorted(ws.plugin_include))}")
    if ws.plugin_exclude:
        click.echo(f"  Blocked plugins: {', '.join(sorted(ws.plugin_exclude))}")
    if ws.startup_agents:
        click.echo(f"  Startup agents: {', '.join(ws.startup_agents)}")
    click.echo(f"  Preload plugins: {ws.preload_plugins}")
    click.echo("Workspace compiled successfully. Use -w flag to apply at startup.")


# ---------------------------------------------------------------------------
# Template subcommands
# ---------------------------------------------------------------------------


@main.group()
def template() -> None:
    """Manage templates (list, inspect)."""


@template.command("list")
def template_list() -> None:
    """List available templates from specs directory."""
    from obscura.core.compiler.loader import load_specs_dir

    specs_dir = resolve_obscura_specs_dir()
    if not specs_dir.is_dir():
        click.echo(f"No specs directory at {specs_dir}")
        return

    registry = load_specs_dir(specs_dir)
    if not registry.templates:
        click.echo("No templates found.")
        return

    for name, tmpl in sorted(registry.templates.items()):
        extends = f"extends={tmpl.spec.extends}" if tmpl.spec.extends else ""
        plugins = ", ".join(tmpl.spec.plugins) if tmpl.spec.plugins else "(none)"
        click.echo(f"  {name:20s}  {extends:20s}  plugins=[{plugins}]")


@template.command("inspect")
@click.argument("name")
def template_inspect(name: str) -> None:
    """Inspect a template (with inheritance resolved)."""
    from obscura.core.compiler.errors import CompileError
    from obscura.core.compiler.loader import load_specs_dir
    from obscura.core.compiler.merger import merge_template_chain
    from obscura.core.compiler.resolver import resolve_template_chain

    specs_dir = resolve_obscura_specs_dir()
    registry = load_specs_dir(specs_dir)

    tmpl = registry.get_template(name)
    if tmpl is None:
        click.echo(f"Template '{name}' not found.", err=True)
        return

    try:
        chain = resolve_template_chain(tmpl, registry)
        merged = merge_template_chain(chain)
    except CompileError as exc:
        _log.debug("suppressed exception in template_inspect", exc_info=True)
        click.echo(f"Resolution error: {exc}", err=True)
        return

    spec = merged.spec
    click.echo(f"Template: {merged.metadata.name}")
    if merged.metadata.description:
        click.echo(f"  Description: {merged.metadata.description}")
    if merged.metadata.tags:
        click.echo(f"  Tags: {', '.join(merged.metadata.tags)}")
    click.echo(f"  Provider: {spec.provider}")
    if spec.model_id:
        click.echo(f"  Model: {spec.model_id}")
    click.echo(f"  Agent type: {spec.agent_type}")
    click.echo(f"  Max iterations: {spec.max_iterations}")
    if spec.plugins:
        click.echo(f"  Plugins: {', '.join(spec.plugins)}")
    if spec.capabilities:
        click.echo(f"  Capabilities: {', '.join(spec.capabilities)}")
    if spec.tool_allowlist is not None:
        click.echo(f"  Tool allowlist: {', '.join(spec.tool_allowlist)}")
    if spec.tool_denylist:
        click.echo(f"  Tool denylist: {', '.join(spec.tool_denylist)}")
    if spec.instructions:
        preview = spec.instructions[:200]
        if len(spec.instructions) > 200:
            preview += "..."
        click.echo(f"  Instructions: {preview}")


# ---------------------------------------------------------------------------
# Kairos goal runtime CLI -- registered as `obscura kairos <subcommand>`
# ---------------------------------------------------------------------------

from obscura.cli.kairos_commands import kairos_group as _kairos_group  # noqa: E402

main.add_command(_kairos_group)


# ---------------------------------------------------------------------------
# WhatsApp (wuzapi) CLI -- registered as `obscura whatsapp <subcommand>`
# ---------------------------------------------------------------------------

from obscura.cli.whatsapp_commands import whatsapp_group as _whatsapp_group  # noqa: E402

main.add_command(_whatsapp_group)


# ---------------------------------------------------------------------------
# Full-screen TUI subcommand — `obscura tui`
# ---------------------------------------------------------------------------


@main.command(name="tui")
@click.option(
    "-b",
    "--backend",
    default="copilot",
    type=click.Choice([b.value for b in Backend]),
    help="Backend to use.",
)
@click.option("-m", "--model", default=None, help="Model ID override.")
@click.option("-s", "--system", default="", help="System prompt.")
@click.option("--session", "session_id", default=None, help="Resume session by ID.")
@click.option("--max-turns", default=10, type=int, help="Max agent loop turns.")
@click.option(
    "--tools",
    default="on",
    type=click.Choice(["on", "off"]),
    help="Enable/disable tool calling.",
)
@click.option(
    "--confirm/--no-confirm",
    default=False,
    help="Require approval before each tool call.",
)
@click.option(
    "--no-default-prompt",
    is_flag=True,
    default=False,
    help="Skip the default Obscura system prompt.",
)
@click.option(
    "--supervise/--no-supervise",
    default=True,
    help="Launch the agent fleet from agents.yaml (default: on).",
)
@click.option(
    "--full-screen/--no-full-screen",
    default=True,
    help="Use the full-screen Application; --no-full-screen falls back "
    "to the legacy bordered REPL (useful for dumb terminals / CI).",
)
@click.option(
    "--show-thinking/--hide-thinking",
    default=True,
    help="Render THINKING_DELTA blocks inline (Ctrl-T still expands them when hidden).",
)
@click.option(
    "--log-level",
    "log_level",
    default="WARNING",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Console log level.",
)
def tui(  # noqa: PLR0913 — Click options are individual params on purpose.
    backend: str,
    model: str | None,
    system: str,
    session_id: str | None,
    max_turns: int,
    tools: str,
    confirm: bool,
    no_default_prompt: bool,
    supervise: bool,
    full_screen: bool,
    show_thinking: bool,
    log_level: str,
) -> None:
    """Launch the full-screen prompt-toolkit TUI.

    Same engine as the bordered REPL (``obscura``), different surface:
    persistent input box at the bottom, scrollback above, dedicated
    rows for live status, notifications, and sticky banners. Tool
    approvals appear as modal floats. Slash commands work as in the
    REPL — output is captured into the transcript.
    """
    from obscura.cli.tui.engine_adapter import TUIEngineConfig
    from obscura.cli.tui.runtime import run_tui

    sync_guide_files()
    sync_provider_settings()

    cfg = TUIEngineConfig(
        backend=backend,
        model=model,
        system=system,
        session_id=session_id,
        max_turns=max_turns,
        tools_enabled=(tools == "on"),
        confirm_enabled=confirm,
        no_default_prompt=no_default_prompt,
        supervise=supervise,
        full_screen=full_screen,
        show_thinking=show_thinking,
        log_level=cast(
            "Literal['DEBUG', 'INFO', 'WARNING', 'ERROR']",
            log_level.upper(),
        ),
    )
    try:
        exit_code = asyncio.run(run_tui(cfg))
    except KeyboardInterrupt:
        # 130 is the conventional shell exit code for SIGINT; the user
        # hit Ctrl-C, that's expected and not worth a traceback.
        _log.debug("tui interrupted by SIGINT", exc_info=True)
        exit_code = 130
    raise SystemExit(exit_code)


# ---------------------------------------------------------------------------
# Network gateway subcommand — `obscura gateway`
# ---------------------------------------------------------------------------


@main.command(name="gateway")
@click.option("--host", default=None, help="Bind address (default from config).")
@click.option("--port", default=None, type=int, help="Listen port (default from config).")
@click.option(
    "--backend",
    default=None,
    help="LLM backend to use for gateway sessions (default from config).",
)
@click.option("--token", default=None, help="Bearer token override (default: auto-loaded).")
@click.option(
    "--reload",
    is_flag=True,
    default=False,
    help="Enable uvicorn auto-reload (development mode).",
)
def gateway(
    host: str | None,
    port: int | None,
    backend: str | None,
    token: str | None,
    reload: bool,
) -> None:
    """Start the Obscura network gateway.

    Exposes an HTTP agent endpoint on the network so remote clients can
    connect to Obscura over the wire.  Token auth is required; the token
    is printed at startup (masked) so you can hand it to callers.

    Analogous to OpenClaw's gateway on port 18789, but for Obscura.
    """
    import uvicorn

    from obscura.core.config import ObscuraConfig
    from obscura.integrations.a2a.token_manager import A2ATokenManager

    cfg = ObscuraConfig.load()

    _host = host or cfg.network_gateway_host
    _port = port or cfg.network_gateway_port
    _backend = backend or cfg.network_gateway_backend

    # Resolve token: CLI flag > env/file auto-load
    if token:
        _token = token
    elif cfg.network_gateway_token:
        _token = cfg.network_gateway_token
    else:
        _token = A2ATokenManager().load_network_gateway_token()

    # Masked token display: first 8 chars + ***
    masked = _token[:8] + "***" if len(_token) >= 8 else "***"

    click.echo(f"Obscura Network Gateway listening on http://{_host}:{_port}")
    click.echo(f"  Backend : {_backend}")
    click.echo(f"  Token   : {masked}")
    click.echo(
        "  Connect : Authorization: Bearer <token>  "
        "(set OBSCURA_NETWORK_TOKEN or use ~/.obscura/network-gateway.token)"
    )

    try:
        from obscura.integrations.network_gateway import create_gateway_app
        from obscura.integrations.network_gateway.config import GatewayConfig

        # Start from full config (picks up standalone_agent_*, tailscale, etc.)
        # then apply CLI flag overrides on top.
        gw_cfg = GatewayConfig.from_obscura_config()
        gw_cfg = GatewayConfig(
            **{
                **gw_cfg.__dict__,
                "host": _host,
                "port": _port,
                "agent_backend": _backend,
                "token": _token,
            }
        )
        app = create_gateway_app(gw_cfg)
    except ImportError:
        click.echo(
            "network_gateway module not yet available — "
            "run with --reload once the module is installed.",
            err=True,
        )
        raise SystemExit(1)

    uvicorn.run(
        app,
        host=_host,
        port=_port,
        reload=reload,
    )


# Backwards-compat aliases added by test harness
def _emit_context_warnings(*args: Any, **kwargs: Any) -> Any:  # pyright: ignore[reportUnusedFunction]
    from .warnings import emit_context_warnings as _impl

    return _impl(*args, **kwargs)


def _copilot_budget_pct(tokens: int, context_window: int) -> Any:  # pyright: ignore[reportUnusedFunction]
    from .warnings import get_copilot_budget_pct as _impl

    return _impl(tokens, context_window)


def _parse_confirm_decision(answer: str) -> str | None:  # pyright: ignore[reportUnusedFunction]
    a = (answer or "").lower()
    if "approve" in a or a.strip().startswith("yes") or "accept" in a:
        return "approve"
    if "deny" in a or a.strip().startswith("no") or "do not" in a or "dont" in a:
        return "deny"
    return None
