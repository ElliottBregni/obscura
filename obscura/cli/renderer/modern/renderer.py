"""obscura.cli.renderer.modern.renderer — Frame-buffered modern renderer.

Implements ``RendererProtocol`` with a **live region** approach:

- **Committed output** (completed text blocks, tool results, thinking
  panels) is printed once and never redrawn.
- **Live region** (spinner, streaming text with cursor, thinking preview)
  occupies the bottom of the output and is erased + redrawn every frame
  at 30 FPS using ``\\033[A`` (cursor-up) sequences.

This gives visible animations (typing cursor, pulsing spinner, elapsed
timer) while staying compatible with ``patch_stdout``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import sys
import textwrap
import time
from typing import Any

from obscura.cli.renderer.modern.layout import BorderStyle
from obscura.cli.renderer.modern.theme import (
    MUTED,
    OK_COLOR,
    RESET,
    STYLE_ACCENT,
    STYLE_DEFAULT,
    STYLE_DIM,
    STYLE_ERROR,
    STYLE_THINKING,
    STYLE_TOOL,
    STYLE_WARN,
    THINKING_COLOR,
    Style,
)
from obscura.cli.renderer.modern.layout import get_border_chars
from obscura.core.types import AgentEvent, AgentEventKind


# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_PULSE_COLORS = [201, 165, 129, 93, 129, 165]
_ERASE_LINE = "\033[2K"
_CURSOR_UP = "\033[A"
_HIDE_CURSOR = "\033[?25l"
_SHOW_CURSOR = "\033[?25h"


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


def _styled(text: str, style: Style) -> str:
    """Wrap text in ANSI style sequences."""
    if style == STYLE_DEFAULT:
        return text
    return f"{style.ansi()}{text}{RESET}"


def _wrap(text: str, width: int) -> list[str]:
    """Word-wrap text to width, preserving newlines."""
    if width <= 0:
        return []
    raw_lines = text.split("\n")
    wrapped: list[str] = []
    for raw in raw_lines:
        if not raw:
            wrapped.append("")
        else:
            wrapped.extend(textwrap.wrap(raw, width=width) or [""])
    return wrapped


# ---------------------------------------------------------------------------
# ModernRenderer
# ---------------------------------------------------------------------------


class ModernRenderer:
    """Modern renderer with animated live region.

    Architecture:
    - ``_commit(lines)`` — print lines permanently (above the live region)
    - ``_live_region`` — 0–N lines at the bottom, erased and redrawn each frame
    - ``_frame_loop`` — async task at 30 FPS that rebuilds + redraws the live region
    """

    FRAME_INTERVAL_S: float = 1.0 / 30  # 30 FPS

    def __init__(self, streaming_status: object | None = None) -> None:
        self._ss = streaming_status
        self._out = sys.stdout
        self._width = shutil.get_terminal_size((80, 24)).columns

        # Committed output tracking
        self._text_accum: list[str] = []
        self._thinking_blocks: list[str] = []
        self._thinking_buf: list[str] = []
        self._in_thinking = False

        # Live region state
        self._live_lines_count: int = 0  # how many lines the live region occupies
        self._spinner_idx: int = 0
        self._spinner_text: str = ""
        self._spinner_visible: bool = False
        self._spinner_start: float = 0.0
        self._dot_phase: int = 0
        self._pulse_idx: int = 0

        # Streaming text with reveal cursor
        self._stream_buf: list[str] = []
        self._reveal_pos: int = 0
        self._cursor_visible: bool = True
        self._cursor_blink_counter: int = 0
        self._chars_per_frame: int = 12

        # Frame loop
        self._dirty = False
        self._frame_task: asyncio.Task[None] | None = None
        self._finished = False
        self._frame_count: int = 0

        # Session context for status bar
        self._session_title: str = ""
        self._session_model: str = ""
        self._session_ctx_pct: int = 0

        # Tool renderer registry (lazy)
        self._tool_registry: Any = None

        # FPS override
        fps_str = os.environ.get("OBSCURA_RENDERER_FPS", "")
        if fps_str:
            with contextlib.suppress(ValueError):
                self.FRAME_INTERVAL_S = 1.0 / max(1, int(fps_str))

        # SIGWINCH
        with contextlib.suppress(OSError, ValueError):
            signal.signal(signal.SIGWINCH, self._on_resize)

    # ── RendererProtocol ──────────────────────────────────────────────────

    def handle(self, event: AgentEvent) -> None:
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
                self._commit_lines([_styled(f"  error: {_sanitize(event.text)}", STYLE_ERROR)])
            case AgentEventKind.CONTEXT_COMPACT:
                self._commit_lines([_styled(f"  ⚡ {_sanitize(event.text)}", STYLE_WARN)])
            case _:
                pass

        self._dirty = True

    def finish(self) -> None:
        self._flush_all()
        self._erase_live_region()
        self._finished = True
        if self._frame_task is not None:
            self._frame_task.cancel()
            self._frame_task = None
        self._stop_spinner()
        self._write(_SHOW_CURSOR)

    def get_accumulated_text(self) -> str:
        return "".join(self._text_accum)

    def get_thinking_blocks(self) -> list[str]:
        return list(self._thinking_blocks)

    def get_last_thinking(self) -> str:
        return self._thinking_blocks[-1] if self._thinking_blocks else ""

    def set_session_context(
        self,
        *,
        title: str = "",
        model: str = "",
        ctx_pct: int = 0,
    ) -> None:
        """Update session metadata shown in the streaming status bar."""
        if title:
            self._session_title = title
        if model:
            self._session_model = model
        if ctx_pct:
            self._session_ctx_pct = ctx_pct
        self._dirty = True

    # ── Event handlers ────────────────────────────────────────────────────

    def _handle_turn_start(self) -> None:
        self._flush_text()
        self._in_thinking = True
        self._thinking_buf.clear()
        self._start_spinner("thinking")

    def _handle_thinking_delta(self, event: AgentEvent) -> None:
        if not self._in_thinking:
            self._flush_text()
            self._in_thinking = True
        self._thinking_buf.append(event.text)
        # Update toolbar preview
        preview = "".join(self._thinking_buf).strip().replace("\n", " ")
        if len(preview) > 80:
            preview = "..." + preview[-77:]
        self._update_status(preview=preview)

    def _handle_text_delta(self, event: AgentEvent) -> None:
        if self._in_thinking:
            self._flush_thinking()
        self._stop_spinner()
        self._stream_buf.append(event.text)
        self._text_accum.append(event.text)

    def _handle_tool_call(self, event: AgentEvent) -> None:
        self._flush_all()
        try:
            from obscura.cli.tool_summaries import summarize_tool_call
            summary = summarize_tool_call(event.tool_name, event.tool_input)
        except Exception:
            summary = f"{event.tool_name}()"

        self._commit_lines([
            "",
            _styled(f"  ▶ {_sanitize(summary)}", STYLE_TOOL),
        ])
        self._start_spinner(f"running {_sanitize(summary)}")

    def _handle_tool_result(self, event: AgentEvent) -> None:
        self._stop_spinner()

        # Try per-tool renderer for structured output
        registry = self._get_tool_registry()
        lines = registry.render_result_lines(event, self._width)
        if lines is not None:
            self._commit_lines(lines)
            return

        raw = event.tool_result or ""
        if event.is_error:
            snippet = _sanitize(raw[:200]).replace("\n", " ")
            self._commit_lines([_styled(f"  ✘ {snippet}", STYLE_ERROR)])
        else:
            snippet = _sanitize(raw[:120]).replace("\n", " ")
            if len(snippet) > 80:
                snippet = snippet[:77] + "..."
            self._commit_lines([_styled(f"  ✔ {snippet}", Style(fg=OK_COLOR, dim=True))])

    # ── Flush helpers ─────────────────────────────────────────────────────

    def _flush_text(self) -> None:
        """Commit streaming text permanently."""
        self._erase_live_region()
        # Snap reveal to end
        full_text = "".join(self._stream_buf)
        if full_text.strip():
            rule = _styled("─" * self._width, Style(fg=MUTED, dim=True))
            lines = [rule] + _wrap(_sanitize(full_text), self._width)
            self._commit_lines(lines)
        self._stream_buf.clear()
        self._reveal_pos = 0

    def _flush_thinking(self) -> None:
        """Commit thinking buffer as a bordered panel."""
        self._erase_live_region()
        if self._thinking_buf:
            text = "".join(self._thinking_buf).strip()
            if text:
                self._thinking_blocks.append(_sanitize(text))
                panel_lines = self._render_thinking_panel(_sanitize(text), committed=True)
                self._commit_lines(panel_lines)
            self._thinking_buf.clear()
        self._in_thinking = False

    def _flush_all(self) -> None:
        self._flush_thinking()
        self._flush_text()

    # ── Spinner ───────────────────────────────────────────────────────────

    def _start_spinner(self, text: str) -> None:
        self._spinner_visible = True
        self._spinner_text = text
        self._spinner_start = time.monotonic()
        self._spinner_idx = 0
        self._dot_phase = 0
        self._update_status(active=True, text=f"{text}...")

    def _stop_spinner(self) -> None:
        if self._spinner_visible:
            self._spinner_visible = False
            self._erase_live_region()
        self._update_status(active=False, text="", preview="")

    # ── Committed output ──────────────────────────────────────────────────

    def _commit_lines(self, lines: list[str]) -> None:
        """Print lines permanently above the live region."""
        self._erase_live_region()
        for line in lines:
            self._write(line + "\n")

    # ── Live region ───────────────────────────────────────────────────────

    def _erase_live_region(self) -> None:
        """Erase the live region by moving cursor up and clearing lines."""
        if self._live_lines_count > 0:
            # Move up and erase each line
            self._write(
                (_CURSOR_UP + _ERASE_LINE) * self._live_lines_count
                + "\r"
            )
            self._live_lines_count = 0

    def _build_live_region(self) -> list[str]:
        """Build the current live region lines (not yet committed)."""
        lines: list[str] = []
        w = self._width

        # 1. Live thinking panel (pulsing border)
        if self._in_thinking and self._thinking_buf:
            preview_text = "".join(self._thinking_buf).strip()
            if preview_text:
                panel = self._render_thinking_panel(
                    _sanitize(preview_text), committed=False,
                )
                lines.extend(panel)

        # 2. Streaming text with reveal cursor
        full_text = "".join(self._stream_buf)
        if full_text:
            revealed = _sanitize(full_text[: self._reveal_pos])
            wrapped = _wrap(revealed, w)
            if wrapped:
                # Rule above text
                lines.append(_styled("─" * w, Style(fg=MUTED, dim=True)))
                for line_text in wrapped:
                    lines.append(line_text)
                # Blinking cursor at the reveal edge
                if self._reveal_pos < len(full_text) and self._cursor_visible:
                    if wrapped:
                        last = wrapped[-1]
                        cursor_col = len(last)
                        if cursor_col < w:
                            # Replace last line to add cursor
                            lines[-1] = last + _styled("▌", STYLE_ACCENT)

        # 3. Session status bar (shown during active streaming/thinking)
        if (self._spinner_visible or self._in_thinking or self._stream_buf) and self._session_title:
            status_parts: list[str] = []
            if self._session_title:
                status_parts.append(self._session_title)
            if self._session_model:
                status_parts.append(self._session_model)
            if self._session_ctx_pct > 0:
                status_parts.append(f"{self._session_ctx_pct}% context")
            status_text = "  " + " · ".join(status_parts)
            if len(status_text) > w:
                status_text = status_text[:w]
            lines.append(_styled(status_text, Style(fg=MUTED, dim=True)))

        # 4. Spinner with animated dots + elapsed timer
        if self._spinner_visible:
            spinner_char = _SPINNER_FRAMES[self._spinner_idx % len(_SPINNER_FRAMES)]
            dots = "." * (self._dot_phase + 1)
            base = self._spinner_text.rstrip(".")
            elapsed = time.monotonic() - self._spinner_start
            timer = ""
            if elapsed >= 1.0:
                if elapsed < 60:
                    timer = f"  {elapsed:.1f}s"
                else:
                    m, s = divmod(int(elapsed), 60)
                    timer = f"  {m}m{s:02d}s"

            spinner_line = (
                _styled(f"  {spinner_char} ", STYLE_ACCENT)
                + _styled(f"{base}{dots}", STYLE_DIM)
                + _styled(timer, Style(fg=MUTED, dim=True))
            )
            lines.append(spinner_line)

        return lines

    def _render_thinking_panel(self, text: str, *, committed: bool) -> list[str]:
        """Render a thinking block as a bordered panel.

        When ``committed=False``, the border color pulses.
        """
        w = self._width
        inner_w = max(0, w - 4)  # border + padding

        if committed:
            color = THINKING_COLOR
        else:
            color = _PULSE_COLORS[self._pulse_idx % len(_PULSE_COLORS)]

        border_s = Style(fg=color)
        title_s = Style(fg=color, bold=True)
        content_s = STYLE_THINKING

        h_char, v_char, tl, tr, bl, br = get_border_chars(BorderStyle.ROUND)

        # Wrap content
        content_lines = _wrap(text, inner_w)
        # Limit preview to 6 lines when live
        if not committed and len(content_lines) > 6:
            content_lines = content_lines[-6:]

        lines: list[str] = []
        # Top border
        title = " reasoning "
        lines.append(
            _styled(tl, border_s)
            + _styled(title, title_s)
            + _styled(h_char * max(0, w - 2 - len(title)), border_s)
            + _styled(tr, border_s)
        )
        # Content
        for cl in content_lines:
            padded = cl + " " * max(0, inner_w - len(cl))
            lines.append(
                _styled(f"{v_char} ", border_s)
                + _styled(padded, content_s)
                + _styled(f" {v_char}", border_s)
            )
        # Bottom border
        lines.append(
            _styled(bl + h_char * (w - 2) + br, border_s)
        )
        return lines

    def _draw_live_region(self) -> None:
        """Erase old live region and draw the new one."""
        self._erase_live_region()
        lines = self._build_live_region()
        if not lines:
            return
        self._write(_HIDE_CURSOR)
        output = "\n".join(lines) + "\n"
        self._write(output)
        self._live_lines_count = len(lines)
        self._write(_SHOW_CURSOR)

    # ── Frame loop ────────────────────────────────────────────────────────

    def _ensure_frame_task(self) -> None:
        if self._frame_task is not None or self._finished:
            return
        try:
            loop = asyncio.get_running_loop()
            self._frame_task = loop.create_task(self._frame_loop())
        except RuntimeError:
            pass

    async def _frame_loop(self) -> None:
        """Background task: animate and redraw the live region at target FPS."""
        try:
            while not self._finished:
                self._frame_count += 1
                animation_active = False

                # Advance text reveal
                full_len = len("".join(self._stream_buf))
                if self._reveal_pos < full_len:
                    backlog = full_len - self._reveal_pos
                    burst = self._chars_per_frame
                    if backlog > 200:
                        burst = max(burst, backlog // 4)
                    elif backlog > 80:
                        burst = max(burst, backlog // 6)
                    self._reveal_pos = min(full_len, self._reveal_pos + burst)
                    animation_active = True
                    # Blink cursor every 8 frames
                    if self._frame_count % 8 == 0:
                        self._cursor_visible = not self._cursor_visible

                # Advance spinner
                if self._spinner_visible:
                    self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER_FRAMES)
                    if self._frame_count % 3 == 0:
                        self._dot_phase = (self._dot_phase + 1) % 3
                    animation_active = True

                # Pulse thinking border
                if self._in_thinking and self._thinking_buf:
                    if self._frame_count % 5 == 0:
                        self._pulse_idx = (self._pulse_idx + 1) % len(_PULSE_COLORS)
                    animation_active = True

                # Redraw live region
                if self._dirty or animation_active:
                    self._width = shutil.get_terminal_size((80, 24)).columns
                    self._draw_live_region()
                    self._dirty = False

                await asyncio.sleep(self.FRAME_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    def _render_frame(self) -> None:
        """Synchronous single-frame render (used by finish)."""
        self._draw_live_region()

    # ── I/O helpers ───────────────────────────────────────────────────────

    def _write(self, s: str) -> None:
        try:
            self._out.write(s)
            self._out.flush()
        except Exception:
            pass

    def _on_resize(self, signum: int, frame: Any) -> None:
        self._width = shutil.get_terminal_size((80, 24)).columns
        self._dirty = True

    # ── Toolbar status ────────────────────────────────────────────────────

    def _update_status(
        self,
        *,
        active: bool | None = None,
        text: str | None = None,
        preview: str | None = None,
    ) -> None:
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

    # ── Tool registry (lazy) ──────────────────────────────────────────────

    def _get_tool_registry(self) -> Any:
        if self._tool_registry is None:
            from obscura.cli.renderer.modern.tool_renderers import ToolRendererRegistry
            self._tool_registry = ToolRendererRegistry()
        return self._tool_registry
