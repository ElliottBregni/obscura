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
from collections.abc import Awaitable
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout.containers import FloatContainer

from obscura.cli.commands import REPLContext, handle_command
from obscura.cli.commands import COMPLETIONS as _COMMAND_COMPLETIONS
from obscura.cli.promptkit import PROMPT_STYLE, SlashCommandCompleter
from obscura.cli.render import console as rich_console
from obscura.cli.render import set_active_renderer
from obscura.cli.tui.engine_adapter import TUIEngineHandle
from obscura.cli.tui.formatter import format_slash_output
from obscura.cli.tui.layout import build_layout
from obscura.cli.tui.overlays import build_overlays
from obscura.cli.tui.renderer import TUIRenderer
from obscura.cli.tui.state import (
    HUDState,
    LiveRegionKind,
    NotificationItem,
    TUIMode,
    TUIState,
)
from obscura.cli.renderer.channels import Severity
from obscura.core.db_factory import DatabaseFactory
from obscura.core.enums.agent import AgentEventKind
from obscura.core.event_store import EventStoreProtocol

logger = logging.getLogger(__name__)

__all__ = ["ObscuraTUIApp"]


# Notification keys reused so successive presses replace rather than stack.
_NOTIF_KEY_HELP = "tui.help"
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

        # Overlays first so we can attach their callables onto the engine
        # handle before the layout consults them. ``command_names`` is a
        # zero-arg callable so the palette picks up ``set_secret_menu_visibility``
        # additions without rebuilding the overlay.
        self._overlays = build_overlays(
            self._state,
            command_names=lambda: sorted(_COMMAND_COMPLETIONS.keys()),
        )
        self._wire_overlay_callbacks()

        # Layout consumes a slash-command completer + an on_submit hook.
        self._completer = SlashCommandCompleter(_COMMAND_COMPLETIONS)
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

        # Lazy-initialised on first slash command — avoids creating a DB
        # connection for every TUI launch when the user never runs one.
        self._slash_event_store: EventStoreProtocol | None = None

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
        hud = HUDState(
            backend=cfg.backend,
            model=cfg.model or "(default)",
            session_id=handle.session.session_id,
            session_title=None,
            workspace=cfg.workspace,
            mode=TUIMode.CHAT,
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
        if not isinstance(floats_container, FloatContainer):
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
            self._overlays.command_palette.open()
            self._invalidate()

        @kb.add("f1")
        def _show_help(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
            self._state.push_notification(
                NotificationItem(
                    title="hotkeys",
                    body=(
                        "Ctrl-D quit · Ctrl-C cancel · Ctrl-K palette · "
                        "F1 help · F2 toggle-agents · Esc+Enter newline"
                    ),
                    severity=Severity.INFO,
                    source="tui",
                    key=_NOTIF_KEY_HELP,
                    ttl_seconds=8.0,
                )
            )
            self._invalidate()

        @kb.add("f2")
        def _toggle_agents(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
            self._state.show_agent_panel = not self._state.show_agent_panel
            self._invalidate()

        return Application(
            layout=self._layout.layout,
            key_bindings=kb,
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
        if buffer is not None:
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
        """Spin up the spinner-tick + notification-pruner tasks."""
        spinner_task = asyncio.create_task(self._spinner_tick(), name="tui.spinner")
        pruner_task = asyncio.create_task(
            self._notification_prune(), name="tui.notif-prune"
        )
        self._background_tasks = [spinner_task, pruner_task]
        # Register with the session so its LIFO teardown also sweeps these
        # if the app exits via an unexpected path.
        with contextlib.suppress(Exception):
            self._handle.session.register_resource(spinner_task, name="tui.spinner")
            self._handle.session.register_resource(pruner_task, name="tui.notif-prune")

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
            return

    async def _notification_prune(self) -> None:
        """Drop expired toast notifications every second."""
        try:
            while True:
                await asyncio.sleep(1.0)
                removed = self._state.prune_notifications()
                if removed:
                    self._invalidate()
        except asyncio.CancelledError:
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
        """Build a minimal :class:`REPLContext` for slash-command dispatch.

        The TUI doesn't replay the full :class:`REPLContext` lifecycle —
        it just synthesizes the fields :func:`handle_command` reads when
        invoking a slash command. Anything a command writes back onto the
        context is intentionally discarded; commands that *need* to
        mutate session state (``/backend``, ``/clear``) still reach the
        live :class:`AgentSession` via ``ctx.client``.
        """
        if self._slash_event_store is None:
            self._slash_event_store = DatabaseFactory.create_event_store()
        return REPLContext(
            client=self._handle.session,
            store=self._slash_event_store,
            session_id=self._handle.session_id,
            backend=self._handle.config.backend,
            model=self._handle.config.model,
            system_prompt=self._handle.config.system,
            max_turns=self._handle.config.max_turns,
            tools_enabled=self._handle.config.tools_enabled,
            mcp_configs=[],
            confirm_enabled=self._handle.config.confirm_enabled,
        )

    # ------------------------------------------------------------------
    # Awaitable typing helper (kept for clarity at call sites)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_awaitable(obj: Any) -> bool:
        return isinstance(obj, Awaitable)
