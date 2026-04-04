"""obscura.cli.renderer.modern.renderer — Frame-buffered modern renderer.

Implements ``RendererProtocol`` with:
- Frame-buffered rendering at configurable FPS (default 30)
- Component tree updated by event dispatch
- Inline mode (default) compatible with ``patch_stdout``
- Optional fullscreen mode via alt-screen
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import sys
from typing import Any

from obscura.cli.renderer.modern.alt_screen import AltScreenManager
from obscura.cli.renderer.modern.components import (
    PanelComponent,
    RootComponent,
    RuleComponent,
    SpinnerComponent,
    StreamingTextComponent,
    TextComponent,
    ToolCallComponent,
)
from obscura.cli.renderer.modern.compositor import Compositor
from obscura.cli.renderer.modern.frame_buffer import FrameBuffer
from obscura.cli.renderer.modern.layout import BorderStyle
from obscura.cli.renderer.modern.theme import (
    MUTED,
    STYLE_ACCENT,
    STYLE_DEFAULT,
    STYLE_DIM,
    STYLE_ERROR,
    STYLE_THINKING,
    STYLE_WARN,
    THINKING_COLOR,
    Style,
)
from obscura.core.types import AgentEvent, AgentEventKind


def _sanitize(s: str) -> str:
    """Strip ANSI escapes and control characters."""
    if not s:
        return ""
    cleaned = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", s)
    cleaned = re.sub(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)", "", cleaned)
    cleaned = re.sub(r"\x1B[PX^_][^\x1B]*(?:\x1B\\|$)", "", cleaned)
    cleaned = re.sub(r"\x1B[@-Z\\-_]", "", cleaned)
    cleaned = re.sub(r"\x1B", "", cleaned)
    return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]+", "", cleaned)


class ModernRenderer:
    """Frame-buffered renderer implementing ``RendererProtocol``.

    Events are dispatched to a component tree.  A background asyncio
    task renders dirty frames at the target FPS.  In inline mode (the
    default), output is append-only for ``patch_stdout`` compatibility.
    """

    FRAME_INTERVAL_S: float = 1.0 / 30  # 30 FPS

    def __init__(self, streaming_status: object | None = None) -> None:
        self._ss = streaming_status

        # Terminal dimensions
        ts = shutil.get_terminal_size((80, 24))
        self._width = ts.columns
        self._height = ts.lines

        # Alt-screen
        self._alt = AltScreenManager()
        fullscreen = self._alt.should_start_fullscreen()
        if fullscreen:
            self._alt.enter()

        # Frame buffer
        self._buf = FrameBuffer(
            self._width,
            self._height,
            fullscreen=fullscreen,
        )

        # Component tree
        self._root = RootComponent()
        self._text_stream = StreamingTextComponent(text_style=STYLE_DEFAULT)
        self._thinking_panel = PanelComponent(
            title="reasoning",
            border=BorderStyle.ROUND,
            border_color=THINKING_COLOR,
            content_style=STYLE_THINKING,
        )
        self._thinking_panel.visible = False
        self._spinner = SpinnerComponent(
            spinner_style=STYLE_ACCENT,
            text_style=STYLE_DIM,
        )
        self._spinner.visible = False

        # Compositor
        self._compositor = Compositor()

        # Tracking
        self._text_accum: list[str] = []
        self._thinking_blocks: list[str] = []
        self._thinking_buf: list[str] = []
        self._in_thinking = False
        self._dirty = False
        self._frame_task: asyncio.Task[None] | None = None
        self._finished = False

        # Tool renderer registry (lazy import to avoid circular deps)
        self._tool_registry: Any = None

        # FPS override from env
        fps_str = os.environ.get("OBSCURA_RENDERER_FPS", "")
        if fps_str:
            with contextlib.suppress(ValueError):
                self.FRAME_INTERVAL_S = 1.0 / max(1, int(fps_str))

        # SIGWINCH for terminal resize
        with contextlib.suppress(OSError, ValueError):
            signal.signal(signal.SIGWINCH, self._on_resize)

    # -- RendererProtocol --------------------------------------------------

    def handle(self, event: AgentEvent) -> None:
        """Dispatch an event to the component tree."""
        self._ensure_frame_task()

        match event.kind:
            case AgentEventKind.TURN_START:
                self._handle_turn_start()

            case AgentEventKind.THINKING_DELTA:
                self._handle_thinking_delta(event)

            case AgentEventKind.TEXT_DELTA:
                self._handle_text_delta(event)

            case AgentEventKind.TOOL_CALL:
                self._handle_tool_call(event)

            case AgentEventKind.TOOL_RESULT:
                self._handle_tool_result(event)

            case AgentEventKind.TURN_COMPLETE | AgentEventKind.AGENT_DONE:
                self._flush_thinking()
                self._flush_text()
                self._stop_spinner()

            case AgentEventKind.ERROR:
                self._flush_all()
                self._stop_spinner()
                self._add_error(event.text)

            case AgentEventKind.CONTEXT_COMPACT:
                self._add_warning(f"⚡ {_sanitize(event.text)}")

            case _:
                pass

        self._dirty = True

    def finish(self) -> None:
        """Flush everything and tear down the frame loop."""
        self._flush_all()
        self._render_frame()
        self._finished = True

        if self._frame_task is not None:
            self._frame_task.cancel()
            self._frame_task = None

        self._stop_spinner()

        if self._alt.active:
            self._alt.exit()

    def get_accumulated_text(self) -> str:
        return "".join(self._text_accum)

    def get_thinking_blocks(self) -> list[str]:
        return list(self._thinking_blocks)

    def get_last_thinking(self) -> str:
        return self._thinking_blocks[-1] if self._thinking_blocks else ""

    # -- Event handlers ----------------------------------------------------

    def _handle_turn_start(self) -> None:
        self._flush_text()
        self._in_thinking = True
        self._thinking_panel.clear()
        self._thinking_panel.visible = True

        # Start spinner
        self._spinner.text = "thinking..."
        self._spinner.visible = True

        # Update toolbar status
        self._update_status(active=True, text="thinking...")

    def _handle_thinking_delta(self, event: AgentEvent) -> None:
        if not self._in_thinking:
            self._flush_text()
            self._in_thinking = True
            self._thinking_panel.clear()
            self._thinking_panel.visible = True

        self._thinking_buf.append(event.text)
        self._thinking_panel.append(_sanitize(event.text))

        # Update toolbar preview
        preview = "".join(self._thinking_buf).strip().replace("\n", " ")
        if len(preview) > 80:
            preview = "..." + preview[-77:]
        self._update_status(preview=preview)

    def _handle_text_delta(self, event: AgentEvent) -> None:
        if self._in_thinking:
            self._flush_thinking()

        self._stop_spinner()
        self._text_stream.append(event.text)
        self._text_accum.append(event.text)

    def _handle_tool_call(self, event: AgentEvent) -> None:
        self._flush_all()

        # Get summary from tool_summaries
        try:
            from obscura.cli.tool_summaries import summarize_tool_call

            summary = summarize_tool_call(event.tool_name, event.tool_input)
        except Exception:
            summary = f"{event.tool_name}()"

        # Try per-tool renderer
        registry = self._get_tool_registry()
        call_component = registry.render_call(event)
        if call_component is not None:
            self._root.add_child(call_component)
        else:
            # Fallback: generic tool call component
            tc = ToolCallComponent(summary=_sanitize(summary), status="running")
            self._root.add_child(tc)

        # Update spinner
        self._spinner.text = f"running {_sanitize(summary)}..."
        self._spinner.visible = True
        self._update_status(active=True, text=f"running {_sanitize(summary)}...")

    def _handle_tool_result(self, event: AgentEvent) -> None:
        self._stop_spinner()

        registry = self._get_tool_registry()
        result_component = registry.render_result(event)

        if result_component is not None:
            self._root.add_child(result_component)
        else:
            # Fallback: one-line result snippet
            raw = event.tool_result or ""
            is_err = event.is_error

            if is_err:
                snippet = _sanitize(raw[:200]).replace("\n", " ")
                tc = ToolCallComponent(summary=snippet, status="error")
            else:
                snippet = _sanitize(raw[:120]).replace("\n", " ")
                if len(snippet) > 80:
                    snippet = snippet[:77] + "..."
                tc = ToolCallComponent(summary=snippet, status="done")
            self._root.add_child(tc)

    # -- Flush helpers -----------------------------------------------------

    def _flush_text(self) -> None:
        """Commit accumulated streaming text to the component tree."""
        text = self._text_stream.text
        if text.strip():
            # Add a rule separator before text blocks
            self._root.add_child(
                RuleComponent(
                    char="─",
                    rule_style=Style(fg=MUTED, dim=True),
                ),
            )
            # Commit the text as a TextComponent
            self._root.add_child(
                TextComponent(
                    text=_sanitize(text),
                    text_style=STYLE_DEFAULT,
                ),
            )
        self._text_stream.clear()

    def _flush_thinking(self) -> None:
        """Commit accumulated thinking to the component tree."""
        if self._thinking_buf:
            thinking_text = "".join(self._thinking_buf).strip()
            if thinking_text:
                self._thinking_blocks.append(_sanitize(thinking_text))
                # Create a new panel for this block
                panel = PanelComponent(
                    title="reasoning",
                    border=BorderStyle.ROUND,
                    border_color=THINKING_COLOR,
                    content_style=STYLE_THINKING,
                )
                panel.append(_sanitize(thinking_text))
                self._root.add_child(panel)
            self._thinking_buf.clear()
        self._in_thinking = False
        self._thinking_panel.visible = False

    def _flush_all(self) -> None:
        self._flush_thinking()
        self._flush_text()

    def _add_error(self, msg: str) -> None:
        self._root.add_child(
            TextComponent(
                text=f"  error: {_sanitize(msg)}",
                text_style=STYLE_ERROR,
            ),
        )

    def _add_warning(self, msg: str) -> None:
        self._root.add_child(
            TextComponent(
                text=f"  {_sanitize(msg)}",
                text_style=STYLE_WARN,
            ),
        )

    # -- Spinner / toolbar -------------------------------------------------

    def _stop_spinner(self) -> None:
        self._spinner.visible = False
        self._update_status(active=False, text="", preview="")

    def _update_status(
        self,
        *,
        active: bool | None = None,
        text: str | None = None,
        preview: str | None = None,
    ) -> None:
        """Push updates to the StreamingStatus toolbar object."""
        if self._ss is None:
            return
        try:
            if hasattr(self._ss, "update"):
                payload: dict[str, Any] = {}
                if active is not None:
                    payload["active"] = active
                if text is not None:
                    payload["text"] = text
                if preview is not None:
                    payload["preview"] = preview
                if payload:
                    self._ss.update(payload)  # type: ignore[union-attr]
            else:
                if active is not None:
                    self._ss.active = active  # type: ignore[union-attr]
                if text is not None:
                    self._ss.text = text  # type: ignore[union-attr]
                if preview is not None:
                    self._ss.preview = preview  # type: ignore[union-attr]
        except Exception:
            pass

    # -- Frame loop --------------------------------------------------------

    def _ensure_frame_task(self) -> None:
        """Lazily start the background frame rendering task."""
        if self._frame_task is not None or self._finished:
            return
        try:
            loop = asyncio.get_running_loop()
            self._frame_task = loop.create_task(self._frame_loop())
        except RuntimeError:
            # No running event loop — render synchronously
            pass

    async def _frame_loop(self) -> None:
        """Background task: render dirty frames at target FPS."""
        try:
            while not self._finished:
                if self._dirty:
                    self._render_frame()
                    self._dirty = False
                    # Advance spinner
                    if self._spinner.visible:
                        self._spinner.advance()
                await asyncio.sleep(self.FRAME_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    def _render_frame(self) -> None:
        """Composite component tree and flush to terminal."""
        ts = shutil.get_terminal_size((80, 24))
        self._width = ts.columns
        self._height = ts.lines

        # Resize buffer if needed
        if self._buf.width != self._width or self._buf.height != self._height:
            self._buf.resize(self._width, self._height)

        # Build a frame-local root that includes committed components + live state
        frame_root = RootComponent()
        frame_root.children = list(self._root.children)

        # Add live thinking panel if active
        if self._thinking_panel.visible and self._thinking_panel.content:
            frame_root.add_child(self._thinking_panel)

        # Add live text stream if active
        text = self._text_stream.text
        if text.strip():
            frame_root.add_child(
                RuleComponent(
                    char="─",
                    rule_style=Style(fg=MUTED, dim=True),
                ),
            )
            frame_root.add_child(
                TextComponent(
                    text=_sanitize(text),
                    text_style=STYLE_DEFAULT,
                ),
            )

        # Add spinner if active
        if self._spinner.visible:
            frame_root.add_child(self._spinner)

        # Reset buffer for new frame
        self._buf.reset_inline()

        # Measure total height needed
        _w, total_h = frame_root.measure(self._width)
        if total_h > self._height:
            # Expand buffer to fit content
            self._buf.resize(self._width, total_h + 1)

        # Composite into buffer
        self._compositor.composite(
            frame_root,
            self._buf,
            self._width,
            max(self._height, total_h + 1),
        )

        # Flush to terminal
        with contextlib.suppress(Exception):
            self._buf.diff_and_flush(sys.stdout)

    # -- Resize handler ----------------------------------------------------

    def _on_resize(self, signum: int, frame: Any) -> None:
        """Handle SIGWINCH: update dimensions and mark dirty."""
        ts = shutil.get_terminal_size((80, 24))
        self._width = ts.columns
        self._height = ts.lines
        self._dirty = True

    # -- Tool registry (lazy) ----------------------------------------------

    def _get_tool_registry(self) -> Any:
        if self._tool_registry is None:
            from obscura.cli.renderer.modern.tool_renderers import ToolRendererRegistry

            self._tool_registry = ToolRendererRegistry()
        return self._tool_registry
