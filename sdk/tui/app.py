"""
sdk.tui.app -- ObscuraTUI main Textual application.

The central application class that composes the TUI layout, manages
mode switching, routes user input to the backend bridge, and handles
slash commands, streaming responses, and session lifecycle.

Launch via::

    obscura-sdk tui
    obscura-sdk tui --backend copilot --mode code --cwd ./myproject
    obscura-sdk tui --session abc123
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive

from sdk.tui.backend_bridge import BackendBridge
from sdk.tui.modes import FileChange, ModeManager, Plan, TUIMode
from sdk.tui.session import ConversationTurn, TUISession
from sdk.tui.themes import DARK_THEME, LIGHT_THEME
from sdk.tui.widgets.diff_view import DiffView
from sdk.tui.widgets.input_area import PromptInput, SlashCommand
from sdk.tui.widgets.message_bubble import MessageBubble
from sdk.tui.widgets.message_list import MessageList
from sdk.tui.widgets.plan_view import PlanView
from sdk.tui.widgets.sidebar import Sidebar
from sdk.tui.widgets.status_bar import StatusBar


# ---------------------------------------------------------------------------
# ObscuraTUI Application
# ---------------------------------------------------------------------------

class ObscuraTUI(App):
    """Main Textual application for the Obscura TUI.

    Provides a Claude Code-style interactive experience with streaming
    responses, inline diffs, mode switching, tool use visualization,
    and session persistence.
    """

    CSS = DARK_THEME

    BINDINGS = [
        Binding("ctrl+a", "switch_mode('ask')", "Ask", show=True),
        Binding("ctrl+p", "switch_mode('plan')", "Plan", show=True),
        Binding("ctrl+e", "switch_mode('code')", "Code", show=True),
        Binding("ctrl+d", "switch_mode('diff')", "Diff", show=True),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar", show=True),
        Binding("ctrl+n", "new_session", "New Session", show=True),
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("escape", "cancel_stream", "Cancel", show=False),
    ]

    # Reactive state
    dark_mode: reactive[bool] = reactive(True)

    # -- Init ---------------------------------------------------------------

    def __init__(
        self,
        backend: str = "claude",
        model: str | None = None,
        cwd: str | None = None,
        session_id: str | None = None,
        initial_mode: str = "ask",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.title = "Obscura TUI"

        # Core components
        self._bridge = BackendBridge(
            backend=backend,
            model=model,
            cwd=cwd,
        )
        self._mode_manager = ModeManager(
            initial=TUIMode(initial_mode),
        )

        # Session
        self._session: TUISession | None = None
        self._session_id_to_resume = session_id
        self._cwd = cwd or "."

        # Widget references (set in on_mount)
        self._sidebar: Sidebar | None = None
        self._message_list: MessageList | None = None
        self._input_area: PromptInput | None = None
        self._status_bar: StatusBar | None = None
        self._diff_view: DiffView | None = None
        self._plan_view: PlanView | None = None

        # Streaming state
        self._current_bubble: MessageBubble | None = None
        self._assistant_text: str = ""

    # -- Layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Sidebar(id="sidebar")
        yield MessageList(id="message-list")
        yield PromptInput(id="input-area", cwd=self._cwd)
        yield StatusBar(id="status-bar")

    async def on_mount(self) -> None:
        """Initialize the app after mounting."""
        # Cache widget references
        self._sidebar = self.query_one("#sidebar", Sidebar)
        self._message_list = self.query_one("#message-list", MessageList)
        self._input_area = self.query_one("#input-area", PromptInput)
        self._status_bar = self.query_one("#status-bar", StatusBar)

        # Set initial mode
        mode = self._mode_manager.current
        self._input_area.set_mode(mode)
        self._sidebar.update_mode(mode)
        self._status_bar.update_mode(mode)
        self._status_bar.update_backend(
            self._bridge.backend_name,
            self._bridge.model,
        )

        # Register mode switch listener
        self._mode_manager.on_switch(self._on_mode_switched)

        # Session setup
        if self._session_id_to_resume:
            try:
                self._session = TUISession.load(self._session_id_to_resume)
                self._restore_session()
                self._show_system(
                    f"Resumed session {self._session.session_id}"
                )
            except FileNotFoundError:
                self._show_system(
                    f"Session {self._session_id_to_resume} not found, "
                    "starting new session."
                )
                self._session = TUISession(
                    backend=self._bridge.backend_name,
                    model=self._bridge.model,
                )
        else:
            self._session = TUISession(
                backend=self._bridge.backend_name,
                model=self._bridge.model,
            )

        self._update_session_display()

        # Connect to backend
        self.run_worker(self._connect_backend(), exclusive=True)

        # Focus input
        self._input_area.focus_input()

    # -- Backend connection -------------------------------------------------

    async def _connect_backend(self) -> None:
        """Connect to the backend asynchronously."""
        try:
            self._show_system(
                f"Connecting to {self._bridge.backend_name}..."
            )
            await self._bridge.connect()
            self._show_system("Connected.")
        except Exception as e:
            self._show_system(f"Connection error: {e}")

    # -- Mode switching -----------------------------------------------------

    def action_switch_mode(self, mode: str) -> None:
        """Switch between ask/plan/code/diff modes."""
        try:
            new_mode = TUIMode(mode)
        except ValueError:
            self._show_system(f"Unknown mode: {mode}")
            return

        self._mode_manager.switch(new_mode)

    def _on_mode_switched(self, old: TUIMode, new: TUIMode) -> None:
        """Callback when mode changes."""
        # Update all widgets
        if self._input_area:
            self._input_area.set_mode(new)
        if self._sidebar:
            self._sidebar.update_mode(new)
        if self._status_bar:
            self._status_bar.update_mode(new)

        # Update system prompt on bridge
        self._bridge.update_system_prompt(
            self._mode_manager.get_system_prompt()
        )

        # Show mode change in message list
        self._show_system(f"Switched to {new.value.upper()} mode")

        # Update sidebar files in code/diff mode
        if new in (TUIMode.CODE, TUIMode.DIFF) and self._sidebar:
            self._sidebar.update_files(self._mode_manager.pending_changes)

    # -- Input handling -----------------------------------------------------

    async def on_prompt_input_submitted(
        self, event: PromptInput.Submitted
    ) -> None:
        """Handle user prompt submission."""
        prompt = event.text
        if not prompt:
            return

        # Add user message to UI
        if self._message_list:
            self._message_list.add_user_message(prompt)

        # Add to session
        if self._session:
            self._session.add_user_turn(
                content=prompt,
                mode=self._mode_manager.current,
            )

        # Route based on mode
        mode = self._mode_manager.current

        if mode == TUIMode.PLAN:
            await self._handle_plan_prompt(prompt)
        elif mode == TUIMode.CODE:
            await self._handle_code_prompt(prompt)
        elif mode == TUIMode.DIFF:
            await self._handle_diff_prompt(prompt)
        else:
            # ASK mode — default streaming
            await self._stream_response(prompt)

    async def on_prompt_input_slash_command_received(
        self, event: PromptInput.SlashCommandReceived
    ) -> None:
        """Handle slash commands."""
        await self._handle_slash_command(event.command)

    # -- Streaming response -------------------------------------------------

    async def _stream_response(self, prompt: str) -> None:
        """Stream a response from the backend."""
        if not self._bridge.connected:
            self._show_system("Not connected to backend.")
            return

        if self._bridge.streaming:
            self._show_system("Already streaming. Press Escape to cancel.")
            return

        # Create assistant bubble
        if self._message_list:
            self._current_bubble = self._message_list.add_assistant_message()

        self._assistant_text = ""

        # Update status bar
        if self._status_bar:
            self._status_bar.set_streaming(True)

        # Stream via bridge
        try:
            await self._bridge.stream_prompt(
                prompt,
                on_text=self._on_text_delta,
                on_thinking=self._on_thinking_delta,
                on_tool_start=self._on_tool_start,
                on_tool_delta=self._on_tool_delta,
                on_tool_result=self._on_tool_result,
                on_done=self._on_stream_done,
                on_error=self._on_stream_error,
            )
        except Exception as e:
            self._on_stream_error(str(e))

    # -- Stream callbacks ---------------------------------------------------

    def _on_text_delta(self, text: str) -> None:
        """Handle text delta from stream."""
        self._assistant_text += text
        if self._current_bubble:
            self._current_bubble.append_text(text)
        if self._message_list:
            self._message_list.request_scroll_to_bottom()

    def _on_thinking_delta(self, text: str) -> None:
        """Handle thinking delta from stream."""
        if self._current_bubble:
            block = self._current_bubble.get_thinking_block()
            if block is None:
                block = self._current_bubble.add_thinking_block()
            block.append(text)

    def _on_tool_start(self, tool_name: str) -> None:
        """Handle tool use start."""
        if self._current_bubble:
            self._current_bubble.add_tool_status(tool_name)

    def _on_tool_delta(self, delta: str) -> None:
        """Handle tool input delta."""
        if self._current_bubble:
            ts = self._current_bubble.get_latest_tool_status()
            if ts:
                ts.update_input(delta)

    def _on_tool_result(self, result: str) -> None:
        """Handle tool result."""
        if self._current_bubble:
            ts = self._current_bubble.get_latest_tool_status()
            if ts:
                ts.complete(result)

    def _on_stream_done(self) -> None:
        """Handle stream completion."""
        if self._current_bubble:
            self._current_bubble.finalize()

        if self._status_bar:
            self._status_bar.set_streaming(False)
            self._status_bar.update_timing(self._bridge.last_duration)

        # Save assistant turn to session
        if self._session and self._assistant_text:
            self._session.add_assistant_turn(
                content=self._assistant_text,
                mode=self._mode_manager.current,
                metadata={"duration": self._bridge.last_duration},
            )

        # If in Plan mode, try to parse the plan
        if self._mode_manager.current == TUIMode.PLAN:
            self._try_parse_plan(self._assistant_text)

        # Clear current bubble reference
        self._current_bubble = None
        if self._message_list:
            self._message_list.clear_current()

    def _on_stream_error(self, error: str) -> None:
        """Handle stream error."""
        if self._current_bubble:
            self._current_bubble.show_error(error)
            self._current_bubble.finalize()

        if self._status_bar:
            self._status_bar.set_streaming(False)

        self._current_bubble = None
        if self._message_list:
            self._message_list.clear_current()

    # -- Plan mode ----------------------------------------------------------

    async def _handle_plan_prompt(self, prompt: str) -> None:
        """Handle prompt in Plan mode."""
        await self._stream_response(prompt)

    def _try_parse_plan(self, text: str) -> None:
        """Attempt to parse a structured plan from assistant response."""
        plan = Plan.parse(text)
        if plan.steps:
            self._mode_manager.active_plan = plan
            self._show_system(
                f"Plan parsed: {len(plan.steps)} steps. "
                "Use /mode code to execute after approving."
            )

    # -- Code mode ----------------------------------------------------------

    async def _handle_code_prompt(self, prompt: str) -> None:
        """Handle prompt in Code mode."""
        await self._stream_response(prompt)

    # -- Diff mode ----------------------------------------------------------

    async def _handle_diff_prompt(self, prompt: str) -> None:
        """Handle prompt in Diff mode."""
        await self._stream_response(prompt)

    # -- Slash command handling ---------------------------------------------

    async def _handle_slash_command(self, cmd: SlashCommand) -> None:
        """Process a parsed slash command."""
        handler = {
            "mode": self._cmd_mode,
            "backend": self._cmd_backend,
            "model": self._cmd_model,
            "session": self._cmd_session,
            "clear": self._cmd_clear,
            "memory": self._cmd_memory,
            "diff": self._cmd_diff,
            "help": self._cmd_help,
            "quit": self._cmd_quit,
        }.get(cmd.command)

        if handler:
            await handler(cmd.args)
        else:
            self._show_system(f"Unknown command: /{cmd.command}")

    async def _cmd_mode(self, args: list[str]) -> None:
        """Handle /mode <ask|plan|code|diff>."""
        if not args:
            self._show_system(
                f"Current mode: {self._mode_manager.current.value}. "
                "Usage: /mode <ask|plan|code|diff>"
            )
            return
        self.action_switch_mode(args[0])

    async def _cmd_backend(self, args: list[str]) -> None:
        """Handle /backend <name>."""
        from sdk._types import Backend
        supported = {b.value for b in Backend}
        if not args:
            self._show_system(
                f"Current backend: {self._bridge.backend_name}. "
                f"Usage: /backend <{'|'.join(sorted(supported))}>"
            )
            return
        backend = args[0]
        if backend not in supported:
            self._show_system(
                f"Unknown backend: {backend}. "
                f"Available: {', '.join(sorted(supported))}"
            )
            return
        self._show_system(f"Switching to {backend}...")
        try:
            await self._bridge.switch_backend(backend)
            if self._sidebar:
                self._sidebar.update_backend(backend, self._bridge.model)
            if self._status_bar:
                self._status_bar.update_backend(backend, self._bridge.model)
            self._show_system(f"Connected to {backend}.")
        except Exception as e:
            self._show_system(f"Error switching backend: {e}")

    async def _cmd_model(self, args: list[str]) -> None:
        """Handle /model <model-id>."""
        if not args:
            self._show_system(
                f"Current model: {self._bridge.model or 'default'}. "
                "Usage: /model <model-id>"
            )
            return
        model_id = args[0]
        self._show_system(f"Switching model to {model_id}...")
        try:
            await self._bridge.switch_backend(
                self._bridge.backend_name, model=model_id
            )
            if self._sidebar:
                self._sidebar.update_backend(
                    self._bridge.backend_name, model_id
                )
            if self._status_bar:
                self._status_bar.update_backend(
                    self._bridge.backend_name, model_id
                )
            self._show_system(f"Model set to {model_id}.")
        except Exception as e:
            self._show_system(f"Error switching model: {e}")

    async def _cmd_session(self, args: list[str]) -> None:
        """Handle /session <new|list|load <id>>."""
        if not args:
            self._show_system("Usage: /session <new|list|load <id>>")
            return

        subcmd = args[0]

        if subcmd == "new":
            await self._new_session()
        elif subcmd == "list":
            sessions = TUISession.list_sessions()
            if not sessions:
                self._show_system("No saved sessions.")
            else:
                for s in sessions[:10]:
                    turns = s.get("turn_count", 0)
                    sid = s["session_id"]
                    updated = s.get("updated_at", "")[:10]
                    self._show_system(
                        f"  {sid}  ({turns} turns, {updated})"
                    )
        elif subcmd == "load":
            if len(args) < 2:
                self._show_system("Usage: /session load <id>")
                return
            sid = args[1]
            try:
                self._session = TUISession.load(sid)
                self._restore_session()
                self._show_system(f"Loaded session {sid}")
            except FileNotFoundError:
                self._show_system(f"Session not found: {sid}")
        else:
            self._show_system(f"Unknown session command: {subcmd}")

    async def _cmd_clear(self, args: list[str]) -> None:
        """Handle /clear."""
        if self._message_list:
            self._message_list.clear_all()
        self._show_system("Conversation cleared.")

    async def _cmd_memory(self, args: list[str]) -> None:
        """Handle /memory <list|get <ns> <key>|set <ns> <key> <value>>."""
        if not args:
            self._show_system(
                "Usage: /memory <list|get <ns> <key>|set <ns> <key> <value>>"
            )
            return
        subcmd = args[0]
        if subcmd == "list":
            await self._memory_list()
        elif subcmd == "get":
            if len(args) < 3:
                self._show_system("Usage: /memory get <namespace> <key>")
                return
            await self._memory_get(args[1], args[2])
        elif subcmd == "set":
            if len(args) < 4:
                self._show_system("Usage: /memory set <namespace> <key> <value>")
                return
            await self._memory_set(args[1], args[2], " ".join(args[3:]))
        else:
            self._show_system(f"Unknown memory command: {subcmd}")

    async def _memory_list(self) -> None:
        """List memory namespaces via the backend API."""
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get("http://localhost:8080/api/v1/memory/namespaces")
                resp.raise_for_status()
                data = resp.json()
            namespaces = data.get("namespaces", [])
            if not namespaces:
                self._show_system("  (no namespaces)")
            else:
                self._show_system(f"Memory namespaces ({len(namespaces)}):")
                for ns in namespaces:
                    self._show_system(f"  {ns}")
        except Exception as e:
            self._show_system(f"Error listing memory: {e}")

    async def _memory_get(self, namespace: str, key: str) -> None:
        """Get a memory value via the backend API."""
        import httpx
        import json

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"http://localhost:8080/api/v1/memory/{namespace}/{key}"
                )
                resp.raise_for_status()
                data = resp.json()
            value = data.get("value", data)
            self._show_system(
                f"{namespace}/{key} = {json.dumps(value, indent=2)}"
            )
        except Exception as e:
            self._show_system(f"Error reading memory: {e}")

    async def _memory_set(self, namespace: str, key: str, value: str) -> None:
        """Set a memory value via the backend API."""
        import httpx
        import json

        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"http://localhost:8080/api/v1/memory/{namespace}/{key}",
                    json={"value": parsed},
                )
                resp.raise_for_status()
            self._show_system(f"Set {namespace}/{key}")
        except Exception as e:
            self._show_system(f"Error setting memory: {e}")

    async def _cmd_diff(self, args: list[str]) -> None:
        """Handle /diff <show|accept-all|reject-all>."""
        if not args:
            self._show_system("Usage: /diff <show|accept-all|reject-all>")
            return
        subcmd = args[0]
        changes = self._mode_manager.pending_changes
        if subcmd == "show":
            if not changes:
                self._show_system("No pending changes.")
            else:
                for c in changes:
                    self._show_system(f"  [{c.status}] {c.path}")
        elif subcmd == "accept-all":
            for c in changes:
                c.status = "accepted"
            self._show_system(f"Accepted {len(changes)} change(s).")
        elif subcmd == "reject-all":
            for c in changes:
                c.status = "rejected"
            self._show_system(f"Rejected {len(changes)} change(s).")
        else:
            self._show_system(f"Unknown diff command: {subcmd}")

    async def _cmd_help(self, args: list[str]) -> None:
        """Handle /help."""
        from sdk._types import Backend
        backends = "|".join(sorted(b.value for b in Backend))
        modes = "|".join(m.value for m in TUIMode)

        help_lines = [
            "Obscura TUI Commands:",
            f"  /mode <{modes}>       Switch mode",
            f"  /backend <{backends}>   Switch backend",
            "  /model <model-id>              Change model",
            "  /session new                   Start new session",
            "  /session list                  List saved sessions",
            "  /session load <id>             Resume session",
            "  /clear                         Clear conversation",
            "  /memory list                   List namespaces",
            "  /memory get <ns> <key>         Read memory value",
            "  /memory set <ns> <key> <val>   Write memory value",
            "  /diff show                     Show pending diffs",
            "  /diff accept-all               Accept all changes",
            "  /diff reject-all               Reject all changes",
            "  /help                          Show this help",
            "  /quit                          Exit TUI",
            "",
            "Keybindings:",
            "  Ctrl+A  Ask mode    Ctrl+P  Plan mode",
            "  Ctrl+E  Code mode   Ctrl+D  Diff mode",
            "  Ctrl+B  Toggle sidebar",
            "  Ctrl+N  New session",
            "  Ctrl+Q  Quit",
            "  Escape  Cancel stream",
            "  Enter   Submit      Shift+Enter  New line",
        ]
        for line in help_lines:
            self._show_system(line)

    async def _cmd_quit(self, args: list[str]) -> None:
        """Handle /quit."""
        await self._shutdown()
        self.exit()

    # -- Actions ------------------------------------------------------------

    def action_toggle_sidebar(self) -> None:
        """Toggle the sidebar visibility."""
        if self._sidebar:
            self._sidebar.toggle_visibility()

    async def action_new_session(self) -> None:
        """Start a new session."""
        await self._new_session()

    async def action_cancel_stream(self) -> None:
        """Cancel the current streaming response."""
        if self._bridge.streaming:
            self._bridge.cancel_stream()
            self._show_system("Stream cancelled.")

    # -- Session management -------------------------------------------------

    async def _new_session(self) -> None:
        """Save current session and start a new one."""
        # Save the current session
        if self._session and self._session.turns:
            self._session.save()
            TUISession.auto_rotate()
            self._show_system(
                f"Saved session {self._session.session_id}"
            )

        # Clear the UI
        if self._message_list:
            self._message_list.clear_all()

        # Create fresh session
        self._session = TUISession(
            backend=self._bridge.backend_name,
            model=self._bridge.model,
        )
        self._update_session_display()
        self._show_system(
            f"New session: {self._session.session_id}"
        )

    def _restore_session(self) -> None:
        """Restore messages from a loaded session into the UI."""
        if not self._session or not self._message_list:
            return

        self._message_list.clear_all()
        for turn in self._session.turns:
            if turn.role == "user":
                self._message_list.add_user_message(turn.content)
            elif turn.role == "assistant":
                bubble = self._message_list.add_assistant_message(
                    turn.content
                )
                bubble.finalize()

        # Restore mode
        if self._session.turns:
            last_mode = self._session.last_mode
            self._mode_manager.switch(last_mode)

    def _update_session_display(self) -> None:
        """Update sidebar and status bar with session info."""
        if not self._session:
            return
        if self._sidebar:
            self._sidebar.update_session(
                self._session.session_id,
                self._session.turn_count,
            )
        if self._status_bar:
            self._status_bar.update_session(self._session.session_id)

    # -- Sidebar mode selection handler ------------------------------------

    async def on_sidebar_mode_selected(
        self, event: Sidebar.ModeSelected
    ) -> None:
        """Handle mode selection from the sidebar."""
        self._mode_manager.switch(event.mode)

    # -- Helpers ------------------------------------------------------------

    def _show_system(self, text: str) -> None:
        """Show a system message in the message list."""
        if self._message_list:
            self._message_list.add_system_message(text)

    # -- Shutdown -----------------------------------------------------------

    async def _shutdown(self) -> None:
        """Clean shutdown: save session, disconnect backend."""
        if self._session and self._session.turns:
            self._session.save()
        await self._bridge.disconnect()

    async def action_quit(self) -> None:
        """Quit the app gracefully."""
        await self._shutdown()
        self.exit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_tui(
    backend: str = "claude",
    model: str | None = None,
    cwd: str | None = None,
    session: str | None = None,
    mode: str = "ask",
) -> None:
    """Launch the Obscura TUI.

    Args:
        backend: Backend to use ('claude' or 'copilot').
        model: Model ID override.
        cwd: Working directory for file operations.
        session: Session ID to resume.
        mode: Initial mode ('ask', 'plan', 'code', 'diff').
    """
    app = ObscuraTUI(
        backend=backend,
        model=model,
        cwd=cwd,
        session_id=session,
        initial_mode=mode,
    )
    app.run()
