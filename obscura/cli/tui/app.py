"""obscura.cli.tui.app — Top-level :class:`prompt_toolkit.Application`.

The :class:`ObscuraTUIApp` glues together every other piece of the TUI:

* the :class:`obscura.cli.tui.state.TUIState` mutable container,
* the :class:`obscura.cli.tui.renderer.TUIRenderer` (events ⇒ state),
* the layout factory in :mod:`obscura.cli.tui.layout` (state ⇒
  prompt-toolkit :class:`Layout`),
* the overlays factory in :mod:`obscura.cli.tui.overlays` (modal floats),
* and the engine handle in :mod:`obscura.cli.tui.engine_adapter`
  (session bootstrap + agent stream).

The user calls :meth:`ObscuraTUIApp.run` and the app drives the entire
session lifecycle until the user exits via ``/quit`` /
``Ctrl-D`` / ``Ctrl-C``.

This module deliberately performs **no lazy imports**. Every dependency
sits at module top so import failures surface at start-up.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import platform
import subprocess
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent, merge_key_bindings
from prompt_toolkit.layout.containers import FloatContainer

from obscura.agent.agents import AgentStatus
from obscura.cli.commands import REPLContext, handle_command
from obscura.cli.commands import COMPLETIONS as _COMMAND_COMPLETIONS
from obscura.cli.promptkit import PROMPT_STYLE, SlashCommandCompleter
from obscura.cli.render import console as rich_console
from obscura.cli.render import set_active_renderer
from obscura.cli.renderer.reveal import compute_reveal_burst
from obscura.cli.tui.engine_adapter import TUIEngineHandle
from obscura.cli.tui.formatter import format_slash_output
from obscura.cli.tui.layout import build_layout
from obscura.cli.tui.overlays import build_overlays
from obscura.cli.tui.renderer import TUIRenderer
from obscura.cli.tui.state import (
    HUDState,
    LiveRegionKind,
    NotificationItem,
    RunningAgentSnapshot,
    TUIMode,
    TUIState,
)
from obscura.cli.renderer.channels import Severity
from obscura.core.db_factory import DatabaseFactory
from obscura.core.enums.agent import AgentEventKind

logger = logging.getLogger(__name__)


def _platform_open_command() -> str:
    """Return the platform's "open this file" command.

    Falls through ``$EDITOR`` / ``$VISUAL`` (handled by the caller)
    when the user has set those; otherwise picks ``open`` on macOS
    and ``xdg-open`` elsewhere. Both are detached-friendly so the TUI
    can keep its terminal.
    """
    return "open" if platform.system() == "Darwin" else "xdg-open"


def _extract_mcp_status(handle: "TUIEngineHandle") -> list[dict[str, Any]]:
    """Snapshot the session's per-server MCP status for the HUD.

    ``install_mcp_servers`` writes structured ``MCPServerStatus``
    dataclasses onto ``session.mcp_status`` (connected / failed /
    unknown plus tool count and the raw error). We translate that
    into plain dicts so the HUDState model can hold them without
    pulling the MCP protocol types into the TUI dependency tree.

    Falls back to deriving "unknown" entries from
    ``session.config.mcp_servers`` when ``mcp_status`` is empty —
    e.g. on Codex (which does its own MCP routing) or on legacy
    sessions built before the status block was wired.
    """
    try:
        statuses = list(getattr(handle.session, "mcp_status", []) or [])
    except Exception:
        logger.debug("tui: read mcp_status failed", exc_info=True)
        statuses = []

    if statuses:
        return [
            {
                "name": getattr(s, "name", "") or "?",
                "state": getattr(s, "state", "unknown"),
                "transport": getattr(s, "transport", ""),
                "tool_count": int(getattr(s, "tool_count", 0) or 0),
                "error": getattr(s, "error", "") or "",
            }
            for s in statuses
        ]

    # Fallback: derive bare "unknown" entries from the configured
    # list. Better than nothing for surfaces that bypass the install
    # block (codex backend, hand-constructed sessions in tests).
    try:
        configs = list(getattr(handle.session.config, "mcp_servers", []) or [])
    except Exception:
        logger.debug("tui: read mcp_servers config failed", exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for idx, cfg in enumerate(configs):
        name = ""
        if isinstance(cfg, dict):
            cfg_dict = cast(dict[str, Any], cfg)
            raw = cfg_dict.get("name")
            if raw:
                name = str(raw).strip()
        elif hasattr(cfg, "name"):
            name = str(getattr(cfg, "name", "") or "").strip()
        if not name:
            name = f"mcp-{idx}"
        out.append(
            {
                "name": name,
                "state": "unknown",
                "transport": "",
                "tool_count": 0,
                "error": "",
            },
        )
    return out


def _ttl_cache_zero_arg[T](
    fn: "Callable[[], T]",
    *,
    ttl_s: float,
) -> "Callable[[], T]":
    """Wrap a no-arg callable with a per-instance TTL cache.

    The ``SlashCommandCompleter`` calls its supplier on every
    keystroke once ``complete_while_typing=True`` is enabled. The raw
    ``REPLContext.discover_*`` methods walk the filesystem each call;
    a 5-second cache turns repeat lookups into a dict read and is
    short enough that newly-installed @commands / $skills appear
    without restart.
    """
    last_value: list[T] = []
    last_at: list[float] = [0.0]

    def _cached() -> T:
        now = time.monotonic()
        if last_value and (now - last_at[0]) < ttl_s:
            return last_value[0]
        value = fn()
        if last_value:
            last_value[0] = value
        else:
            last_value.append(value)
        last_at[0] = now
        return value

    return _cached


__all__ = ["ObscuraTUIApp"]


# Notification keys reused so successive presses replace rather than stack.
_NOTIF_KEY_HELP = "tui.help"
_NOTIF_KEY_FILTER = "tui.transcript-filter"
_NOTIF_KEY_PERM = "tui.perm-mode"
_NOTIF_KEY_CANCEL = "tui.cancel"
_NOTIF_KEY_ERROR = "tui.error"


class ObscuraTUIApp:
    """Full-screen prompt-toolkit Application bound to a TUI engine handle.

    Owns the :class:`TUIState`, the :class:`Application`, the renderer,
    the layout, the overlays, and the runtime async tasks (spinner timer,
    notification pruner). The user calls :meth:`run` and the app drives
    everything until the user exits via ``/quit``, ``Ctrl-D``, or
    ``Ctrl-C``.
    """

    def __init__(self, handle: TUIEngineHandle) -> None:
        """Build state + renderer + overlays + layout + Application.

        Side effects are limited to constructing in-memory objects; no
        async work runs until :meth:`run` is awaited.
        """
        self._handle: TUIEngineHandle = handle
        self._state: TUIState = self._build_state(handle)
        self._renderer: TUIRenderer = TUIRenderer(
            self._state, invalidate=self._invalidate
        )

        # A persistent REPLContext drives BOTH slash-command dispatch and
        # the input completer's ``@command``/``$skill`` discovery. Built
        # once at startup; ``REPLContext.discover_*`` methods are cheap
        # (lazy file-walks via inner loaders) and safe to call per
        # keystroke for completion.
        self._slash_event_store = DatabaseFactory.create_event_store()
        self._repl_ctx: REPLContext = REPLContext(
            client=handle.session,
            store=self._slash_event_store,
            session_id=handle.session_id,
            backend=handle.config.backend,
            model=handle.config.model,
            system_prompt=handle.config.system,
            max_turns=handle.config.max_turns,
            tools_enabled=handle.config.tools_enabled,
            mcp_configs=[],
            confirm_enabled=handle.config.confirm_enabled,
        )

        # Overlays first so we can attach their callables onto the engine
        # handle before the layout consults them. ``command_names`` is a
        # zero-arg callable so the palette picks up
        # ``set_secret_menu_visibility`` additions without rebuilding the
        # overlay. Beyond the slash commands we also surface a small set
        # of TUI-only actions (toggle agent panel, toggle tool filter,
        # show help) — distinguished from slash commands by the ``:``
        # prefix in :meth:`_dispatch_palette_selection`.
        self._overlays = build_overlays(
            self._state,
            command_names=self._palette_entries,
        )
        self._wire_overlay_callbacks()

        # Layout consumes the completer + an on_submit hook. The
        # completer is wired with @command and $skill suppliers so tab
        # completion works for the full input grammar (slash / at /
        # dollar) — matching the bordered REPL.
        #
        # The supplier closures wrap the REPL context's discover methods
        # in a short-TTL cache. With ``complete_while_typing=True`` (set
        # on the input ``TextArea`` in :mod:`obscura.cli.tui.layout`) the
        # completer is consulted on every keystroke; the bare suppliers
        # walk the filesystem each call, which costs ~30-80ms with a
        # warm filesystem cache and shows up as input lag while typing
        # an ``@`` or ``$`` token. The cache lives for 5s, which is far
        # longer than any sane keystroke cadence and short enough that
        # newly-installed skills appear without restart.
        self._completer = SlashCommandCompleter(
            _COMMAND_COMPLETIONS,
            at_command_names=_ttl_cache_zero_arg(
                self._repl_ctx.discover_at_commands,
                ttl_s=5.0,
            ),
            dollar_skill_names=_ttl_cache_zero_arg(
                self._repl_ctx.discover_dollar_skills,
                ttl_s=5.0,
            ),
        )
        self._layout = build_layout(
            self._state,
            completer=self._completer,
            on_submit=self._on_submit_sync,
        )
        self._patch_float_container()

        self._app: Application[int] | None = None
        self._background_tasks: list[asyncio.Task[None]] = []
        self._stream_task: asyncio.Task[None] | None = None
        self._exit_code: int = 0

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def state(self) -> TUIState:
        """Mutable :class:`TUIState` shared with the renderer + layout."""
        return self._state

    async def run(self) -> int:
        """Run the Application + engine concurrently. Returns the exit code.

        Sets up background tasks (spinner timer, notification pruner) and
        registers them with the AgentSession so they tear down with the
        session. The Application owns the foreground task — when its
        :meth:`run_async` completes, :meth:`shutdown` cancels the
        background tasks before returning.
        """
        app = self._build_application()
        self._app = app
        # Make the renderer the "active" one — slash hotkeys
        # (Ctrl-P / Ctrl-T) reach into the active renderer for expand-text.
        set_active_renderer(self._renderer)

        self._spawn_background_tasks()

        logger.info(
            "tui: app starting sid=%s backend=%s",
            self._handle.session_id,
            self._handle.config.backend,
        )

        try:
            self._exit_code = await app.run_async(
                handle_sigint=False,
                set_exception_handler=False,
            )
        except (EOFError, KeyboardInterrupt):
            # Clean exit — Ctrl-D / Ctrl-C at the top-level binding.
            logger.debug("tui: exited via top-level Ctrl-C/Ctrl-D", exc_info=True)
            self._exit_code = 0
        except Exception:
            logger.exception("tui: application crashed")
            self._exit_code = 2
        finally:
            await self.shutdown()
            set_active_renderer(None)
        return self._exit_code

    async def submit_user_input(self, text: str) -> None:
        """Route a submitted prompt to slash-handler or the engine.

        This is the single integration point between the input box and
        the agent stream:

        * lines starting with ``/`` are dispatched through
          :func:`obscura.cli.commands.handle_command`; Rich console
          output is captured and pushed to the transcript as a
          :class:`TranscriptKind.SLASH_OUTPUT` entry.
        * everything else is appended to the transcript as a
          :class:`TranscriptKind.USER` entry, then streamed via
          ``handle.session.stream_loop`` with each event fed into the
          renderer.
        """
        text = text.strip()
        if not text:
            return

        if text.startswith("/"):
            await self._dispatch_slash_command(text)
            return

        await self._stream_prompt(text)

    async def shutdown(self) -> None:
        """Cancel background tasks and detach the active renderer.

        Idempotent. The owning :func:`run_tui` already closes the
        :class:`AgentSession` via its async context manager — this method
        just tears down the per-Application transient tasks.
        """
        for task in self._background_tasks:
            if not task.done():
                task.cancel()
        for task in self._background_tasks:
            with contextlib.suppress(BaseException):
                await task
        self._background_tasks.clear()

        if self._stream_task is not None and not self._stream_task.done():
            self._stream_task.cancel()
            with contextlib.suppress(BaseException):
                await self._stream_task
            self._stream_task = None

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_state(handle: TUIEngineHandle) -> TUIState:
        """Construct the initial :class:`TUIState` from the engine handle."""
        cfg = handle.config
        # Snapshot the registry once at startup so the header reflects
        # the post-composition tool surface (system + plugin + MCP-
        # discovered shadows). The agents-tick poll refreshes this
        # later for backends that hot-register tools mid-session.
        try:
            tool_count = len(handle.session.list_tools())
        except Exception:
            logger.debug("tui: initial tool_count read failed", exc_info=True)
            tool_count = 0
        mcp_servers = _extract_mcp_status(handle)
        hud = HUDState(
            backend=cfg.backend,
            model=cfg.model or "(default)",
            session_id=handle.session.session_id,
            session_title=None,
            workspace=cfg.workspace,
            mode=TUIMode.CHAT,
            tool_count=tool_count,
            mcp_servers=mcp_servers,
        )
        return TUIState(hud=hud, show_thinking=cfg.show_thinking)

    def _wire_overlay_callbacks(self) -> None:
        """Bind the overlay async ``request`` methods onto the engine handle.

        Mirrors them onto ``handle.session.host_callbacks`` so freshly
        built tool calls inside the agent loop see the same callables
        any tool that reads :class:`ToolContext` for ``ask_user``,
        ``plan_approval`` etc. routes through the overlay floats instead
        of the legacy Rich panels.
        """
        ask_user_cb = self._overlays.ask_user.request
        plan_approval_cb = self._overlays.plan_approval.request

        async def _user_interact_cb(message: str, actions: list[str]) -> str:
            """Adapter — render an action picker via the ask-user overlay."""
            joined = ", ".join(actions) if actions else ""
            preamble = f"{message}\n\nactions: {joined}" if joined else message
            answer = await ask_user_cb(preamble)
            answer = (answer or "").strip()
            if not actions:
                return answer
            for label in actions:
                if label.lower() == answer.lower():
                    return label
            return answer

        async def _permission_mode_cb(mode: str) -> None:
            self._update_perm_mode(mode)

        self._handle.ask_user_cb = ask_user_cb
        self._handle.plan_approval_cb = plan_approval_cb
        self._handle.user_interact_cb = _user_interact_cb
        self._handle.permission_mode_cb = _permission_mode_cb

        # Bridge Claude Code's built-in ExitPlanMode through the TUI overlay.
        # The SDK's can_use_tool hook fires before the CLI renders its own
        # (pipe-invisible) approval dialog — we intercept it here.
        try:
            from obscura.providers.claude import ClaudeBackend

            _backend = self._handle.session.backend
            if isinstance(_backend, ClaudeBackend):
                _backend.set_plan_approval_callback(plan_approval_cb)
        except Exception:
            logger.debug(
                "TUI: could not wire plan_approval to ClaudeBackend", exc_info=True
            )

        host = dict(self._handle.session.host_callbacks)
        host["ask_user_callback"] = ask_user_cb
        host["plan_approval_callback"] = plan_approval_cb
        host["user_interact_callback"] = _user_interact_cb
        host["permission_mode_callback"] = _permission_mode_cb
        self._handle.session.host_callbacks = host

    def _patch_float_container(self) -> None:
        """Append the overlay floats onto the layout's :class:`FloatContainer`.

        ``build_layout`` returns a :class:`FloatContainer` populated with
        any layout-owned floats (autocompletion menus, etc.); the overlay
        builder owns its own modal floats and exposes them via
        :meth:`TUIOverlays.floats`. The two lists are concatenated
        in-place so the modal stack ends up on top.
        """
        floats_container = self._layout.floats_container
        overlay_floats = list(self._overlays.floats())
        if not overlay_floats:
            return
        if not isinstance(floats_container, FloatContainer):  # pyright: ignore[reportUnnecessaryIsInstance]
            logger.warning(
                "tui: layout.floats_container is %s, not FloatContainer",
                type(floats_container).__name__,
            )
            return
        existing = list(floats_container.floats)
        floats_container.floats = existing + overlay_floats

    def _build_application(self) -> Application[int]:
        """Construct the :class:`Application` with app-level key bindings."""
        cfg = self._handle.config
        kb = KeyBindings()

        @kb.add("c-d")
        def _exit_app(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
            event.app.exit(result=0)

        @kb.add("c-c")
        def _cancel_stream(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
            self._cancel_current_stream()

        @kb.add("c-k")
        def _open_palette(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
            # Drive the palette via its async ``request`` so the user's
            # selection lands in our dispatcher; ``open`` alone shows
            # the float but throws the result away.
            event.app.create_background_task(self._run_palette())

        @kb.add("f1")
        def _show_help(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
            self._state.push_notification(
                NotificationItem(
                    title="hotkeys",
                    body=(
                        "Ctrl-D quit · Ctrl-C cancel · Ctrl-K palette · "
                        "F1 help · F2 / Ctrl-G toggle-agents · "
                        "Ctrl-T tool-call filter · Esc+Enter newline"
                    ),
                    severity=Severity.INFO,
                    source="tui",
                    key=_NOTIF_KEY_HELP,
                    ttl_seconds=8.0,
                )
            )
            self._invalidate()

        @kb.add("c-t")
        def _toggle_tool_filter(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
            # Cycle ``transcript_filter`` between ``"all"`` and
            # ``"tools_only"``. The filter narrows the transcript
            # window to TOOL_USE / TOOL_RESULT entries so the user can
            # see what the agent actually ran in long sessions where
            # those lines are buried under prose.
            current = self._state.transcript_filter
            new_value = "all" if current == "tools_only" else "tools_only"
            self._state.transcript_filter = new_value
            self._state.push_notification(
                NotificationItem(
                    title="transcript filter",
                    body=(
                        "Showing tool calls only — Ctrl-T to show all"
                        if new_value == "tools_only"
                        else "Showing all transcript entries"
                    ),
                    severity=Severity.INFO,
                    source="tui",
                    key=_NOTIF_KEY_FILTER,
                    ttl_seconds=4.0,
                )
            )
            self._invalidate()

        # Both F2 and Ctrl-G toggle the right-side agent panel; F2 is
        # the discoverable Function-key binding shown on the toolbar,
        # Ctrl-G is the chord for users on keyboards where F-keys are
        # awkward (laptops with media-key overlays, remote sessions
        # that swallow F-row escapes, etc.).
        def _toggle_agents(event: KeyPressEvent) -> None:
            self._state.show_agent_panel = not self._state.show_agent_panel
            self._invalidate()

        kb.add("f2")(_toggle_agents)
        kb.add("c-g")(_toggle_agents)

        return Application(
            layout=self._layout.layout,
            key_bindings=merge_key_bindings([kb] + self._overlays.all_key_bindings()),
            full_screen=cfg.full_screen,
            mouse_support=False,
            style=PROMPT_STYLE,
            refresh_interval=0.1,
        )

    # ------------------------------------------------------------------
    # Submit + stream
    # ------------------------------------------------------------------

    def _on_submit_sync(self, text: str) -> None:
        """Sync hook installed on the input buffer's accept handler.

        The layout factory wires this into ``Buffer.accept_handler`` /
        the input window's ``Enter`` keybinding; we cannot ``await`` from
        there, so schedule the real coroutine on the event loop and
        clear the buffer immediately for snappy UX.
        """
        buffer = self._layout.input_buffer
        # ``input_buffer`` is typed non-Optional but defensive guards
        # are cheap; pyright (correctly) flags the redundancy in
        # strict mode.
        if buffer is not None:  # pyright: ignore[reportUnnecessaryComparison]
            buffer.text = ""
        # Schedule the streaming work via the Application's own task
        # registry. ``Application.create_background_task`` runs the
        # coroutine inside the same asyncio Context as the Application's
        # event loop, which is the Context that owns the ContextVar
        # bindings the agent loop relies on (stream guards, discovered
        # tools, etc.). Using a bare ``loop.create_task`` instead would
        # spawn the task in the parent Context — and when the TUI
        # cancels or exits, generator finalisation runs in yet another
        # Context, raising ``ValueError: Token was created in a different
        # Context`` from ``ContextVar.reset``.
        if self._app is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.debug("tui: submit fired outside event loop, ignoring")
                return
            loop.create_task(self.submit_user_input(text))
            return
        self._app.create_background_task(self.submit_user_input(text))

    # ------------------------------------------------------------------
    # Command palette — slash commands + TUI-only actions
    # ------------------------------------------------------------------

    # TUI-only palette actions. Each maps a label (as displayed in the
    # palette) to the bound method that runs it. The label is prefixed
    # with ``:`` so :meth:`_dispatch_palette_selection` can tell it
    # apart from a slash-command name without splitting on spaces. Add
    # entries here when you want a new keyboard-only TUI feature
    # discoverable through Ctrl-K — they'll show up alongside slash
    # commands in the palette and survive the shared filter box.
    _PALETTE_TUI_ACTIONS: tuple[str, ...] = (
        ":toggle agent panel",
        ":toggle tool-call filter",
        ":open last large output",
        ":diagnose MCP servers",
        ":show hotkeys",
    )

    def _palette_entries(self) -> list[str]:
        """Return the combined slash + TUI-action list shown in the palette.

        The order is: TUI actions first (so power-user toggles surface
        even with a long slash list), then slash commands sorted
        alphabetically. Returned fresh on every palette open so newly
        registered slash commands appear without a restart.
        """
        slash = [f"/{name}" for name in sorted(_COMMAND_COMPLETIONS.keys())]
        return [*self._PALETTE_TUI_ACTIONS, *slash]

    async def _run_palette(self) -> None:
        """Open the palette, await selection, dispatch the result.

        Without this driver, ``CommandPaletteOverlay.open`` shows the
        float but the user's pick falls into an unbound future. We
        ``request`` instead so the future the overlay sets resolves
        into our dispatcher.
        """
        try:
            selection = await self._overlays.command_palette.request()
        except Exception:
            logger.exception("tui: palette request raised")
            return
        if not selection:
            return
        self._dispatch_palette_selection(selection)

    def _dispatch_palette_selection(self, selection: str) -> None:
        """Route a palette selection to the matching action.

        ``selection`` is the raw label (with the leading ``/`` for
        slash commands or ``:`` for TUI actions). TUI actions run
        synchronously; slash commands schedule the existing async
        dispatcher.
        """
        if selection.startswith(":"):
            label = selection
            if label == ":toggle agent panel":
                self._state.show_agent_panel = not self._state.show_agent_panel
                self._invalidate()
                return
            if label == ":toggle tool-call filter":
                current = self._state.transcript_filter
                self._state.transcript_filter = (
                    "all" if current == "tools_only" else "tools_only"
                )
                self._state.push_notification(
                    NotificationItem(
                        title="transcript filter",
                        body=(
                            "Showing tool calls only — Ctrl-T to show all"
                            if self._state.transcript_filter == "tools_only"
                            else "Showing all transcript entries"
                        ),
                        severity=Severity.INFO,
                        source="tui",
                        key=_NOTIF_KEY_FILTER,
                        ttl_seconds=4.0,
                    ),
                )
                self._invalidate()
                return
            if label == ":open last large output":
                self._open_last_overflow()
                return
            if label == ":diagnose MCP servers":
                self._diagnose_mcp_servers()
                return
            if label == ":show hotkeys":
                self._state.push_notification(
                    NotificationItem(
                        title="hotkeys",
                        body=(
                            "Ctrl-D quit · Ctrl-C cancel · Ctrl-K palette · "
                            "F1 help · F2 / Ctrl-G toggle-agents · "
                            "Ctrl-T tool-call filter · Esc+Enter newline"
                        ),
                        severity=Severity.INFO,
                        source="tui",
                        key=_NOTIF_KEY_HELP,
                        ttl_seconds=8.0,
                    ),
                )
                self._invalidate()
                return
            logger.debug("tui: unhandled palette TUI action %r", label)
            return

        # Slash command. Handle async via the existing dispatcher; the
        # leading slash is preserved so the dispatcher's parser sees
        # the full ``/name args`` form.
        line = selection if selection.startswith("/") else f"/{selection}"
        if self._app is None:
            return
        self._app.create_background_task(self._dispatch_slash_command(line))

    def _diagnose_mcp_servers(self) -> None:
        """Append a SYSTEM transcript entry summarising every MCP server.

        One row per configured server: name, transport, state, tool
        count, and the raw error if it failed. Goes in the transcript
        (not a toast) because the error strings are often multi-line
        and the user wants to scroll back to them.
        """
        servers = self._state.hud.mcp_servers
        if not servers:
            self._renderer.push_system_message(
                "No MCP servers configured for this session.",
            )
            return
        lines = ["MCP servers:"]
        for srv in servers:
            name = srv.get("name", "?")
            state = srv.get("state", "unknown")
            transport = srv.get("transport") or "?"
            count = srv.get("tool_count", 0)
            error = srv.get("error", "")
            glyph = {
                "connected": "●",
                "failed": "✗",
                "unknown": "○",
            }.get(state, "?")
            label = (
                f"{glyph} {name}  ({transport}, {state}, {count} tool"
                f"{'s' if count != 1 else ''})"
            )
            lines.append(label)
            if state == "failed" and error:
                # Indent multi-line errors so they read as a block
                # under the server label.
                for err_line in error.splitlines() or [error]:
                    lines.append(f"    {err_line}")
        self._renderer.push_system_message("\n".join(lines))
        self._invalidate()

    def _open_last_overflow(self) -> None:
        """Open the most recent oversized tool result in the user's editor.

        ``maybe_truncate_result`` writes anything over 200 KB to
        ~/.cache/obscura/tool-results/<id>.txt and the renderer stashes
        the path on ``state.last_overflow_path``. This handler picks
        ``$EDITOR`` (falling back to ``open`` on macOS / ``xdg-open``
        elsewhere) and launches it detached so the TUI keeps running.
        """
        path = self._state.last_overflow_path
        if not path:
            self._state.push_notification(
                NotificationItem(
                    title="No cached output",
                    body="No tool result has overflowed in this session yet.",
                    severity=Severity.INFO,
                    source="tui",
                    key="tui.no-overflow",
                    ttl_seconds=4.0,
                ),
            )
            self._invalidate()
            return
        editor = (
            os.environ.get("OBSCURA_EDITOR")
            or os.environ.get("EDITOR")
            or os.environ.get("VISUAL")
            or _platform_open_command()
        )
        try:
            # Detach so the TUI keeps owning the terminal — the editor
            # is expected to be a GUI app or a terminal-spawning
            # wrapper, NOT something that takes over our stdin.
            subprocess.Popen(  # noqa: S603 — editor is user-supplied
                [editor, path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            logger.exception("tui: open last overflow failed")
            self._push_error(f"open failed: {exc} — path: {path}")
            return
        self._state.push_notification(
            NotificationItem(
                title="Opened cached output",
                body=f"{editor} {path}",
                severity=Severity.INFO,
                source="tui",
                key="tui.opened-overflow",
                ttl_seconds=4.0,
            ),
        )
        self._invalidate()

    async def _dispatch_slash_command(self, line: str) -> None:
        """Run a ``/slash`` command, capturing Rich output for the transcript."""
        ctx = self._make_repl_context()
        captured = io.StringIO()
        # The Rich console used by every command writes through
        # ``obscura.cli.render.console``. Redirect its file for the
        # duration of the call so we can hand the output to the
        # transcript formatter instead of stdout.
        original_file = rich_console.file
        try:
            rich_console.file = captured  # type: ignore[assignment]
            result = await handle_command(line, ctx)
        except Exception as exc:
            logger.exception("tui: slash command raised")
            self._push_error(f"slash command error: {exc}")
            return
        finally:
            rich_console.file = original_file  # type: ignore[assignment]

        captured_text = captured.getvalue()
        if captured_text:
            entry = format_slash_output(captured_text)
            self._state.append_transcript(entry)
            self._invalidate()

        if result == "quit":
            self._exit_app(0)

    async def _stream_prompt(self, text: str) -> None:
        """Push the user prompt and stream agent events into the renderer."""
        self._renderer.push_user_prompt(text)
        self._invalidate()

        self._handle.cancel_event.clear()
        self._state.hud.is_streaming = True

        async def _drive() -> None:
            try:
                async for event in self._handle.submit(text):
                    if self._handle.cancel_event.is_set():
                        logger.info("tui: cancel_event set, breaking stream early")
                        self._handle.cancel_event.clear()
                        break
                    self._renderer.handle(event)
                    if event.kind == AgentEventKind.AGENT_DONE:
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("tui: agent stream raised")
                self._push_error(f"agent error: {exc}")
            finally:
                self._renderer.finish()
                self._state.hud.is_streaming = False
                self._invalidate()

        task = asyncio.create_task(_drive(), name="tui.stream")
        self._stream_task = task
        try:
            await task
        except asyncio.CancelledError:
            logger.info("tui: stream task cancelled")
        finally:
            if self._stream_task is task:
                self._stream_task = None

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    def _spawn_background_tasks(self) -> None:
        """Spin up the spinner-tick + reveal-tick + agent-poll + notification-pruner tasks."""
        spinner_task = asyncio.create_task(self._spinner_tick(), name="tui.spinner")
        reveal_task = asyncio.create_task(self._reveal_tick(), name="tui.reveal")
        agents_task = asyncio.create_task(
            self._agents_tick(),
            name="tui.agents",
        )
        pruner_task = asyncio.create_task(
            self._notification_prune(), name="tui.notif-prune"
        )
        self._background_tasks = [
            spinner_task,
            reveal_task,
            agents_task,
            pruner_task,
        ]
        # Register with the session so its LIFO teardown also sweeps these
        # if the app exits via an unexpected path.
        with contextlib.suppress(Exception):
            self._handle.session.register_resource(spinner_task, name="tui.spinner")
            self._handle.session.register_resource(reveal_task, name="tui.reveal")
            self._handle.session.register_resource(agents_task, name="tui.agents")
            self._handle.session.register_resource(pruner_task, name="tui.notif-prune")

    async def _agents_tick(self) -> None:
        """Poll ``REPLContext.runtime.list_agents()`` and refresh the side panel.

        Mirrors the bordered REPL's :func:`_refresh_prompt_status`
        loop. Runs every second — agents are spawned over multi-second
        timescales so a finer cadence buys nothing and just burns
        cycles. Skips the poll entirely when no runtime exists yet
        (it's lazily created by the first ``/agent``-related slash
        command), keeping the panel empty rather than racing the
        creation.
        """
        active_statuses = {
            AgentStatus.RUNNING,
            AgentStatus.WAITING,
            AgentStatus.PENDING,
        }
        try:
            while True:
                await asyncio.sleep(1.0)
                runtime = self._repl_ctx.runtime
                if runtime is None:
                    if self._state.hud.running_agents:
                        self._state.hud.running_agents = []
                        self._invalidate()
                    continue
                snapshots: list[RunningAgentSnapshot] = []
                try:
                    agents = runtime.list_agents()
                except Exception:
                    logger.debug("tui: list_agents failed", exc_info=True)
                    continue
                now = time.monotonic()
                for agent in agents:
                    if agent.status not in active_statuses:
                        continue
                    name = getattr(agent.config, "name", None) or getattr(
                        agent,
                        "id",
                        "agent",
                    )
                    started_at = getattr(agent, "started_at_monotonic", None)
                    elapsed = (
                        max(0.0, now - started_at)
                        if isinstance(started_at, (int, float))
                        else 0.0
                    )
                    last_tool = getattr(agent, "last_tool", "") or ""
                    iteration_count = getattr(agent, "iteration_count", 0) or 0
                    raw_status = (
                        agent.status.value
                        if hasattr(agent.status, "value")
                        else str(agent.status)
                    )
                    # ``RunningAgentSnapshot.status`` is a Literal
                    # ["running", "waiting", "pending"]; the runtime
                    # ``AgentStatus`` enum has more members
                    # (RETIRED / FAILED / etc.) that we filter out
                    # via ``active_statuses`` above. Cast back to
                    # the Literal so pyright is happy.
                    snapshot_status: Literal["running", "waiting", "pending"]
                    snapshot_status = (  # pyright: ignore[reportAssignmentType]
                        raw_status
                    )
                    snapshots.append(
                        RunningAgentSnapshot(
                            name=str(name),
                            status=snapshot_status,
                            elapsed_s=float(elapsed),
                            iteration_count=int(iteration_count),
                            last_tool=str(last_tool),
                        )
                    )
                if snapshots != self._state.hud.running_agents:
                    self._state.hud.running_agents = snapshots
                    self._invalidate()

                # Refresh tool / MCP counters too — cheap, and lets
                # the header reflect tools that get registered mid-
                # session (Copilot's hot-MCP discovery, ``/mcp add``
                # commands, etc.).
                try:
                    new_tool_count = len(
                        self._handle.session.list_tools(),
                    )
                except Exception:
                    logger.debug(
                        "tui: list_tools failed during agents-tick",
                        exc_info=True,
                    )
                    new_tool_count = self._state.hud.tool_count
                if new_tool_count != self._state.hud.tool_count:
                    self._state.hud.tool_count = new_tool_count
                    self._invalidate()
                new_mcp = _extract_mcp_status(self._handle)
                if new_mcp != self._state.hud.mcp_servers:
                    self._state.hud.mcp_servers = new_mcp
                    self._invalidate()
        except asyncio.CancelledError:
            # Background tick task got cancelled (TUI shutting down).
            # Logged at debug so deep logs show the lifecycle; nothing
            # actionable for the user.
            logger.debug("tui: tick task cancelled", exc_info=True)
            return

    async def _spinner_tick(self) -> None:
        """Advance the live-region spinner frame every 100ms while active."""
        try:
            while True:
                await asyncio.sleep(0.1)
                live = self._state.live
                if live.kind == LiveRegionKind.IDLE:
                    continue
                live.spinner_idx = (live.spinner_idx + 1) % 1024
                self._invalidate()
        except asyncio.CancelledError:
            # Background tick task got cancelled (TUI shutting down).
            # Logged at debug so deep logs show the lifecycle; nothing
            # actionable for the user.
            logger.debug("tui: tick task cancelled", exc_info=True)
            return

    async def _reveal_tick(self) -> None:
        """Advance the live-region reveal cursor at ~30 FPS.

        TEXT_DELTA / THINKING_DELTA arrive in arbitrary chunks (one
        token, a paragraph, a 2 KB blob). The renderer seeds the full
        streamed text into ``live.full_text``; this loop advances
        ``live.reveal_pos`` along it with jittered ±30% bursts and
        publishes ``live.preview`` as the visible tail of the revealed
        prefix. Same pacing math the bordered REPL uses, so both
        surfaces feel identical when text streams in.
        """
        # ~30 FPS to match ``ModernRenderer.FRAME_INTERVAL_S``. Base
        # 4 chars/frame is ≈120 char/s baseline, comfortably faster
        # than reading speed but slow enough to feel like typing.
        frame_interval = 1.0 / 30.0
        chars_per_frame = 4
        try:
            while True:
                await asyncio.sleep(frame_interval)
                live = self._state.live
                advanced = False
                if live.kind in (
                    LiveRegionKind.STREAMING,
                    LiveRegionKind.THINKING,
                ):
                    full_len = len(live.full_text)
                    if live.reveal_pos < full_len:
                        burst = compute_reveal_burst(
                            backlog=full_len - live.reveal_pos,
                            base=chars_per_frame,
                        )
                        live.reveal_pos = min(
                            full_len,
                            live.reveal_pos + burst,
                        )
                        live.preview = self._tail_for_live(
                            live.full_text[: live.reveal_pos],
                        )
                        advanced = True
                    else:
                        # Caught up — refresh the preview tail in case
                        # the buffer changed (e.g. ``_flush_text``
                        # cleared it on a previous tick).
                        new_preview = self._tail_for_live(
                            live.full_text[: live.reveal_pos],
                        )
                        if new_preview != live.preview:
                            live.preview = new_preview
                            advanced = True

                # Drain any flush-triggering events the renderer
                # queued behind the reveal cursor. Runs every frame
                # regardless of live-kind so timeouts on a stalled
                # queue still fire when the cursor is idle (e.g. the
                # backend produced zero text and went straight to
                # TURN_COMPLETE).
                drained = self._renderer.drain_pending_events()
                if drained or advanced:
                    self._invalidate()
        except asyncio.CancelledError:
            # Background tick task got cancelled (TUI shutting down).
            # Logged at debug so deep logs show the lifecycle; nothing
            # actionable for the user.
            logger.debug("tui: tick task cancelled", exc_info=True)
            return

    @staticmethod
    def _tail_for_live(text: str, *, limit: int = 80) -> str:
        """Trim ``text`` to a single-line preview suitable for the live row."""
        flat = text.replace("\n", " ").strip()
        if len(flat) <= limit:
            return flat
        return "..." + flat[-(limit - 3) :]

    async def _notification_prune(self) -> None:
        """Drop expired toast notifications every second."""
        try:
            while True:
                await asyncio.sleep(1.0)
                removed = self._state.prune_notifications()
                if removed:
                    self._invalidate()
        except asyncio.CancelledError:
            # Background tick task got cancelled (TUI shutting down).
            # Logged at debug so deep logs show the lifecycle; nothing
            # actionable for the user.
            logger.debug("tui: tick task cancelled", exc_info=True)
            return

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------

    def _invalidate(self) -> None:
        """Best-effort Application invalidate (no-op before run starts)."""
        app = self._app
        if app is None:
            return
        with contextlib.suppress(Exception):
            app.invalidate()

    def _exit_app(self, code: int) -> None:
        """Programmatic exit — used by ``/quit`` and friends."""
        self._exit_code = code
        app = self._app
        if app is None:
            return
        with contextlib.suppress(Exception):
            app.exit(result=code)

    def _cancel_current_stream(self) -> None:
        """Signal the in-flight agent stream to stop and notify the user."""
        if self._stream_task is None or self._stream_task.done():
            return
        self._handle.cancel_event.set()
        self._state.push_notification(
            NotificationItem(
                title="cancelled",
                body="Ctrl-C — stream cancelled",
                severity=Severity.WARN,
                source="tui",
                key=_NOTIF_KEY_CANCEL,
                ttl_seconds=4.0,
            )
        )
        self._invalidate()

    def _update_perm_mode(self, mode: str) -> None:
        """Push a permission-mode change to HUD + transcript."""
        self._state.hud.permission_mode = mode
        self._state.push_notification(
            NotificationItem(
                title="permission mode",
                body=mode,
                severity=Severity.INFO,
                source="tui",
                key=_NOTIF_KEY_PERM,
                ttl_seconds=4.0,
            )
        )
        self._invalidate()

    def _push_error(self, message: str) -> None:
        """Surface a runtime error as a high-severity notification."""
        self._state.push_notification(
            NotificationItem(
                title="error",
                body=message,
                severity=Severity.ERROR,
                source="tui",
                key=_NOTIF_KEY_ERROR,
                ttl_seconds=8.0,
            )
        )
        self._invalidate()

    def _make_repl_context(self) -> REPLContext:
        """Return the persistent :class:`REPLContext` shared across the app.

        Built once in :meth:`__init__`; reused for both slash-command
        dispatch and tab-completion (``@command``/``$skill`` discovery).
        Commands that mutate session state (``/backend``, ``/clear``)
        still reach the live :class:`AgentSession` via ``ctx.client`` —
        their mutations land on the shared context and are visible to
        subsequent commands within the same TUI session.
        """
        return self._repl_ctx

    # ------------------------------------------------------------------
    # Awaitable typing helper (kept for clarity at call sites)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_awaitable(obj: Any) -> bool:
        return isinstance(obj, Awaitable)
