"""
sdk.tui.widgets.status_bar -- Bottom status bar.

Displays current mode, backend/model, session ID, timing info,
and streaming indicator.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from sdk.tui.modes import TUIMode


class StatusBar(Widget):
    """Bottom status bar showing mode, model, session, and timing.

    Layout: [MODE] backend/model | Session: xxxx | Duration: 2.3s | STREAMING
    """

    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        padding: 0 1;
    }
    """

    mode: reactive[TUIMode] = reactive(TUIMode.ASK)
    backend: reactive[str] = reactive("claude")
    model: reactive[str] = reactive("")
    session_id: reactive[str] = reactive("")
    duration: reactive[float] = reactive(0.0)
    streaming: reactive[bool] = reactive(False)
    token_count: reactive[int] = reactive(0)

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id or "status-bar", classes=classes)
        self._mode_widget: Static | None = None
        self._model_widget: Static | None = None
        self._session_widget: Static | None = None
        self._timing_widget: Static | None = None
        self._streaming_widget: Static | None = None

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static("", classes="status-mode", id="sb-mode")
            yield Static(" | ", classes="status-sep")
            yield Static("", classes="status-model", id="sb-model")
            yield Static(" | ", classes="status-sep")
            yield Static("", classes="status-session", id="sb-session")
            yield Static(" | ", classes="status-sep")
            yield Static("", classes="status-timing", id="sb-timing")
            yield Static("", classes="status-streaming", id="sb-streaming")

    def on_mount(self) -> None:
        """Cache widget references and render initial state."""
        try:
            self._mode_widget = self.query_one("#sb-mode", Static)
            self._model_widget = self.query_one("#sb-model", Static)
            self._session_widget = self.query_one("#sb-session", Static)
            self._timing_widget = self.query_one("#sb-timing", Static)
            self._streaming_widget = self.query_one("#sb-streaming", Static)
        except Exception:
            pass
        self._render_all()

    # -- Watchers -----------------------------------------------------------

    def watch_mode(self, value: TUIMode) -> None:
        self._render_mode()

    def watch_backend(self, value: str) -> None:
        self._render_model()

    def watch_model(self, value: str) -> None:
        self._render_model()

    def watch_session_id(self, value: str) -> None:
        self._render_session()

    def watch_duration(self, value: float) -> None:
        self._render_timing()

    def watch_streaming(self, value: bool) -> None:
        self._render_streaming()

    # -- Rendering ----------------------------------------------------------

    def _render_all(self) -> None:
        self._render_mode()
        self._render_model()
        self._render_session()
        self._render_timing()
        self._render_streaming()

    def _render_mode(self) -> None:
        if self._mode_widget:
            self._mode_widget.update(f"[{self.mode.value.upper()}]")

    def _render_model(self) -> None:
        if self._model_widget:
            model_str = self.model or "default"
            self._model_widget.update(f"{self.backend}/{model_str}")

    def _render_session(self) -> None:
        if self._session_widget:
            sid = self.session_id[:8] if self.session_id else "none"
            self._session_widget.update(f"Session: {sid}")

    def _render_timing(self) -> None:
        if self._timing_widget:
            if self.duration > 0:
                self._timing_widget.update(f"{self.duration:.1f}s")
            else:
                self._timing_widget.update("")

    def _render_streaming(self) -> None:
        if self._streaming_widget:
            if self.streaming:
                self._streaming_widget.update(" STREAMING")
            else:
                self._streaming_widget.update("")

    # -- Public API ---------------------------------------------------------

    def update_timing(self, duration: float) -> None:
        """Update the duration display."""
        self.duration = duration

    def set_streaming(self, active: bool) -> None:
        """Set the streaming indicator."""
        self.streaming = active

    def update_mode(self, mode: TUIMode) -> None:
        """Update the displayed mode."""
        self.mode = mode

    def update_backend(self, backend: str, model: str | None = None) -> None:
        """Update the backend/model display."""
        self.backend = backend
        if model is not None:
            self.model = model

    def update_session(self, session_id: str) -> None:
        """Update the session ID display."""
        self.session_id = session_id
