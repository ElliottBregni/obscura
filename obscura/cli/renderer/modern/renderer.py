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
import logging
import os
import platform as _platform
import re
import shutil
import signal
import sys
import textwrap
import time
from enum import Enum
from typing import Any

from obscura.cli.renderer.modern.layout import get_border_chars
from obscura.cli.renderer.modern.theme import (
    ERROR_COLOR,
    MUTED,
    RESET,
    STYLE_ACCENT,
    STYLE_DEFAULT,
    STYLE_DIM,
    STYLE_ERROR,
    STYLE_OK,
    STYLE_THINKING,
    STYLE_WARN,
    THINKING_COLOR,
    TOOL_COLOR,
    Style,
)
from obscura.core.enums.agent import AgentEventKind
from obscura.core.enums.ui import BorderStyle
from obscura.core.types import AgentEvent


# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_PULSE_COLORS = [201, 165, 129, 93, 129, 165]

logger = logging.getLogger(__name__)


_BLACK_CIRCLE = "⏺" if _platform.system() == "Darwin" else "●"
_HOOK = "⎿"  # Claude Code's assistant response prefix
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

        # Notification stack — inline toasts above the status region.
        # Latest at the top; auto-evict when expired. Keyed entries
        # update-in-place (rolling progress, streaming agent output).
        # Sources outside the agent loop (supervisor, daemon, rate
        # limiter) push into this stack via :meth:`add_notification`.
        self._notifications: list[Any] = []  # list[Notification]
        # Banner zone — sticky callouts above status. Replaced or
        # dismissed by id via :meth:`set_banner` / :meth:`dismiss_banner`.
        self._banners: dict[str, Any] = {}  # banner_id → Banner

        # FPS override
        fps_str = os.environ.get("OBSCURA_RENDERER_FPS", "")
        if fps_str:
            with contextlib.suppress(ValueError):
                self.FRAME_INTERVAL_S = 1.0 / max(1, int(fps_str))  # pyright: ignore[reportConstantRedefinition]

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
                # Stream-error during a turn — committed to scrollback for
                # permanent record, but in the v2 left-bar system-notice
                # format so it's visually distinct from assistant text.
                self._flush_all()
                self._stop_spinner()
                self._commit_lines(
                    [
                        _styled("  ▎ ", STYLE_DIM)
                        + _styled("✗  ", STYLE_ERROR)
                        + _styled(
                            f"error · {_sanitize(event.text)}", STYLE_ERROR
                        ),
                    ]
                )
            case AgentEventKind.CONTEXT_COMPACT:
                # Route to banner channel — sticky callout above status,
                # auto-dismisses when caller calls dismiss_banner. Legacy
                # behavior was a one-line commit; banner gives it more
                # visibility and prevents it from scrolling away under
                # subsequent text deltas.
                from obscura.cli.renderer.channels import (
                    Banner,
                    BannerKind,
                )

                self.set_banner(
                    Banner(
                        kind=BannerKind.COMPACTION,
                        title="Context compacted",
                        body=event.text or "",
                        banner_id="compaction",
                    )
                )
            case AgentEventKind.PLAN_APPROVAL_REQUEST:
                # Route to banner channel — persistent until user
                # responds. The action handler clears it via
                # dismiss_banner("plan_approval").
                from obscura.cli.renderer.channels import (
                    Banner,
                    BannerKind,
                )

                self._flush_all()
                self._stop_spinner()
                self.set_banner(
                    Banner(
                        kind=BannerKind.PLAN_APPROVAL,
                        title="Plan approval required",
                        body=event.text or "Approve plan to exit plan mode?",
                        actions=("approve", "reject"),
                        banner_id="plan_approval",
                    )
                )
            case AgentEventKind.TASK_STARTED:
                self._notify_task_started(event)
            case AgentEventKind.TASK_PROGRESS:
                self._notify_task_progress(event)
            case AgentEventKind.TASK_NOTIFICATION:
                self._notify_task_notification(event)
            case AgentEventKind.RATE_LIMIT_WARNING:
                self._notify_rate_limit(event)
            case AgentEventKind.MIRROR_ERROR:
                self._notify_mirror_error(event)
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
            logger.debug("suppressed exception in _handle_tool_call", exc_info=True)
            summary = f"{event.tool_name}()"

        tool_name = _sanitize(event.tool_name or "")
        self._commit_lines(
            [
                "",
                (
                    _styled(f"  {_BLACK_CIRCLE} ", Style(fg=TOOL_COLOR))
                    + _styled(tool_name, Style(fg=TOOL_COLOR, bold=True))
                    + _styled(f"  {_sanitize(summary)}", Style(fg=MUTED))
                ),
            ]
        )
        self._start_spinner(f"{_sanitize(summary)}")

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
            err_lines = _sanitize(raw).split("\n")
            cap = int(os.environ.get("OBSCURA_TOOL_OUTPUT_MAX_LINES", "80")) or len(
                err_lines
            )
            display = err_lines[:cap]
            out: list[str] = [
                _styled("  ✗ ", STYLE_ERROR)
                + _styled(
                    _sanitize(display[0])[: self._width] if display else "",
                    Style(fg=ERROR_COLOR, dim=True),
                ),
            ]
            for ln in display[1:]:
                out.append(
                    _styled(
                        f"    {_sanitize(ln)}"[: self._width],
                        Style(fg=ERROR_COLOR, dim=True),
                    )
                )
            if len(err_lines) > cap:
                out.append(
                    _styled(f"    ... ({len(err_lines) - cap} more lines)", STYLE_DIM)
                )
            self._commit_lines(out)
        else:
            result_lines = _sanitize(raw).split("\n")
            cap = int(os.environ.get("OBSCURA_TOOL_OUTPUT_MAX_LINES", "80")) or len(
                result_lines
            )
            display = result_lines[:cap]
            out = [
                _styled("  ✓ ", STYLE_OK)
                + _styled(
                    display[0][: self._width] if display else "",
                    Style(fg=MUTED, dim=True),
                ),
            ]
            for ln in display[1:]:
                out.append(f"    {ln}"[: self._width])
            if len(result_lines) > cap:
                out.append(
                    _styled(
                        f"    ... ({len(result_lines) - cap} more lines)", STYLE_DIM
                    )
                )
            self._commit_lines(out)

    # ── Flush helpers ─────────────────────────────────────────────────────

    def _flush_text(self) -> None:
        """Commit streaming text permanently."""
        self._erase_live_region()
        full_text = "".join(self._stream_buf)
        if full_text.strip():
            w = self._width
            # Indent response under the ⎿ hook (like Claude Code)
            hook_prefix = _styled(f"  {_HOOK}  ", STYLE_DIM)
            content_lines = _wrap(_sanitize(full_text), max(1, w - 5))
            lines: list[str] = []
            for i, cl in enumerate(content_lines):
                if i == 0:
                    lines.append(hook_prefix + cl)
                else:
                    lines.append("     " + cl)
            lines.append("")
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
                panel_lines = self._render_thinking_panel(
                    _sanitize(text), committed=True
                )
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
            self._write((_CURSOR_UP + _ERASE_LINE) * self._live_lines_count + "\r")
            self._live_lines_count = 0

    def _build_live_region(self) -> list[str]:
        """Build the current live region lines (not yet committed).

        Composition (top → bottom):
            1. Live thinking panel
            2. Streaming text with reveal cursor
            3. Sticky banners (plan_approval, arbiter_kill, etc.)
            4. Notification stack (rate-limit, supervisor heartbeats,
               daemon outputs — auto-evicted when expired)
            5. Session status bar
        """
        lines: list[str] = []
        w = self._width

        # 1. Live thinking panel (pulsing border)
        if self._in_thinking and self._thinking_buf:
            preview_text = "".join(self._thinking_buf).strip()
            if preview_text:
                panel = self._render_thinking_panel(
                    _sanitize(preview_text),
                    committed=False,
                )
                lines.extend(panel)

        # 2. Streaming text with reveal cursor
        full_text = "".join(self._stream_buf)
        if full_text:
            revealed = _sanitize(full_text[: self._reveal_pos])
            wrapped = _wrap(revealed, max(1, w - 5))
            if wrapped:
                hook_prefix = _styled(f"  {_HOOK}  ", STYLE_DIM)
                for i, line_text in enumerate(wrapped):
                    if i == 0:
                        lines.append(hook_prefix + line_text)
                    else:
                        lines.append("     " + line_text)
                # Blinking cursor at the reveal edge
                if self._reveal_pos < len(full_text) and self._cursor_visible:
                    if wrapped:
                        last = wrapped[-1]
                        pad = "     " if len(wrapped) > 1 else hook_prefix
                        cursor_col = len(last)
                        if cursor_col < (w - 5):
                            lines[-1] = (
                                (pad if len(wrapped) > 1 else hook_prefix)
                                + last
                                + _styled("▌", STYLE_ACCENT)
                            )

        # 3a. Banner zone — sticky callouts above status.
        for line in self._render_banners():
            lines.append(line)

        # 3b. Notification stack — inline toasts above status.
        # Evict expired entries first; render whatever remains.
        self._evict_expired_notifications()
        for line in self._render_notifications():
            lines.append(line)

        # 4. Session status bar (shown during active streaming/thinking)
        if (
            self._spinner_visible or self._in_thinking or self._stream_buf
        ) and self._session_title:
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

    # ── System-event notification helpers ────────────────────────────────
    #
    # SDK system messages (TaskStartedMessage / TaskProgressMessage /
    # TaskNotificationMessage / RateLimitEvent / MirrorErrorMessage)
    # surface as AgentEvents with a dedicated kind. We translate each into
    # a typed :class:`Notification` so the channel system renders them in
    # the inline-toast region above the status bar — never interleaved
    # with assistant text. Same-task progress entries share a ``key`` so
    # they roll in place rather than stacking.

    def _notify_task_started(self, event: AgentEvent) -> None:
        from obscura.cli.renderer.channels import Notification, Severity

        task_id = event.tool_use_id or "task"
        self.add_notification(
            Notification(
                title="Task started",
                body=_sanitize(event.text or ""),
                severity=Severity.INFO,
                source="task",
                key=f"task:{task_id}",
                ttl_seconds=8.0,
            )
        )

    def _notify_task_progress(self, event: AgentEvent) -> None:
        from obscura.cli.renderer.channels import Notification, Severity

        task_id = event.tool_use_id or "task"
        body_parts: list[str] = []
        if event.text:
            body_parts.append(_sanitize(event.text))
        if event.tool_name:
            body_parts.append(f"using {_sanitize(event.tool_name)}")
        self.add_notification(
            Notification(
                title="Task progress",
                body=" · ".join(body_parts),
                severity=Severity.INFO,
                source="task",
                key=f"task:{task_id}",
                ttl_seconds=10.0,
            )
        )

    def _notify_task_notification(self, event: AgentEvent) -> None:
        from obscura.cli.renderer.channels import Notification, Severity

        task_id = event.tool_use_id or "task"
        raw = event.raw
        status = ""
        if raw is not None:
            status_val = getattr(raw, "status", "")
            if isinstance(status_val, str):
                status = status_val
        severity = {
            "completed": Severity.SUCCESS,
            "failed": Severity.ERROR,
            "stopped": Severity.WARN,
        }.get(status, Severity.INFO)
        title = f"Task {status}" if status else "Task notification"
        self.add_notification(
            Notification(
                title=title,
                body=_sanitize(event.text or ""),
                severity=severity,
                source="task",
                key=f"task:{task_id}",
                ttl_seconds=15.0,
            )
        )

    def _notify_rate_limit(self, event: AgentEvent) -> None:
        from obscura.cli.renderer.channels import Notification, Severity

        raw = event.raw
        info = getattr(raw, "rate_limit_info", None) if raw is not None else None
        status = ""
        util: float | None = None
        rate_type = ""
        resets_at: int | None = None
        if info is not None:
            status_val = getattr(info, "status", "")
            if isinstance(status_val, str):
                status = status_val
            util_val = getattr(info, "utilization", None)
            if isinstance(util_val, (int, float)):
                util = float(util_val)
            rt_val = getattr(info, "rate_limit_type", "")
            if isinstance(rt_val, str):
                rate_type = rt_val
            ra_val = getattr(info, "resets_at", None)
            if isinstance(ra_val, int):
                resets_at = ra_val
        if not status:
            status = event.text or ""

        severity = {
            "rejected": Severity.ERROR,
            "allowed_warning": Severity.WARN,
        }.get(status, Severity.INFO)

        body_parts: list[str] = []
        if util is not None:
            body_parts.append(f"{int(util * 100)}%")
        if rate_type:
            body_parts.append(rate_type.replace("_", "-"))
        if resets_at:
            now = time.time()
            secs = max(0, int(resets_at - now))
            if secs < 60:
                body_parts.append(f"resets in {secs}s")
            elif secs < 3600:
                body_parts.append(f"resets in {secs // 60}m")
            else:
                h = secs // 3600
                m = (secs % 3600) // 60
                body_parts.append(f"resets in {h}h{m:02d}m")

        title = {
            "rejected": "Rate limit hit",
            "allowed_warning": "Rate limit warning",
        }.get(status, "Rate limit")

        self.add_notification(
            Notification(
                title=title,
                body=" · ".join(body_parts),
                severity=severity,
                source="claude",
                key="rate_limit",
                ttl_seconds=30.0 if status == "rejected" else 12.0,
            )
        )

    def _notify_mirror_error(self, event: AgentEvent) -> None:
        from obscura.cli.renderer.channels import Notification, Severity

        self.add_notification(
            Notification(
                title="Session mirror error",
                body=_sanitize(event.text or "Failed to write mirrored session"),
                severity=Severity.WARN,
                source="session",
                key="mirror_error",
                ttl_seconds=8.0,
            )
        )

    # ── Notification + banner channels ───────────────────────────────────

    def add_notification(self, notification: Any) -> None:
        """Push a :class:`Notification` onto the inline stack.

        If the notification has a non-empty ``key``, it replaces any
        existing entry with the same key (rolling-progress semantics).
        Otherwise it appends to the bottom of the stack. The stack
        renders in insertion order; oldest visible at the top.

        Sources outside the agent loop (supervisor, daemon, rate
        limiter, kairos engine) push here directly. The frame loop
        will redraw the live region on the next tick.
        """
        key = getattr(notification, "key", "") or ""
        if key:
            # Replace any same-key entry, preserving position.
            for i, existing in enumerate(self._notifications):
                if getattr(existing, "key", "") == key:
                    self._notifications[i] = notification
                    self._dirty = True
                    return
        self._notifications.append(notification)
        # Bound the stack — drop oldest expired-soonest if we go over.
        if len(self._notifications) > 8:
            self._notifications.pop(0)
        self._dirty = True

    def set_banner(self, banner: Any) -> None:
        """Pin a sticky :class:`Banner` to the live region.

        ``banner.banner_id`` (or its ``kind`` if id is empty) keys the
        slot. Calling again with the same key replaces; pass an empty
        Banner via :meth:`dismiss_banner` to remove.
        """
        banner_id = getattr(banner, "banner_id", "") or str(
            getattr(banner, "kind", "default")
        )
        self._banners[banner_id] = banner
        self._dirty = True

    def dismiss_banner(self, banner_id_or_kind: str) -> None:
        """Remove a sticky banner. Accepts either banner_id or kind."""
        for key in list(self._banners.keys()):
            if key == banner_id_or_kind:
                self._banners.pop(key, None)
                self._dirty = True

    def _evict_expired_notifications(self) -> None:
        """Drop notifications past their TTL."""
        now = time.monotonic()
        keep = [n for n in self._notifications if getattr(n, "expires_at", 0) > now]
        if len(keep) != len(self._notifications):
            self._notifications = keep
            self._dirty = True

    def _render_notifications(self) -> list[str]:
        """Render the notification stack as inline lines.

        Format: ``  ▎ <icon>  <source> · <title>  —  <body>``.

        The dim left bar visually separates system notices from assistant
        text. The icon is colored by severity; header (source + title) is
        colored to match; body is dim. Empty fields collapse cleanly.
        """
        if not self._notifications:
            return []
        lines: list[str] = []
        for n in self._notifications:
            severity = getattr(n, "severity", "info")
            sev = severity.value if isinstance(severity, Enum) else str(severity)
            icon_style = {
                "info": STYLE_ACCENT,
                "warn": STYLE_WARN,
                "error": STYLE_ERROR,
                "success": STYLE_OK,
            }.get(sev, STYLE_DIM)
            icon = {
                "info": "ℹ",  # noqa: RUF001 — Unicode info icon (parity with ⚠/✗/✓)
                "warn": "⚠",
                "error": "✗",
                "success": "✓",
            }.get(sev, "·")

            source = _sanitize(getattr(n, "source", "") or "")
            title = _sanitize(getattr(n, "title", "") or "")
            body = _sanitize(getattr(n, "body", "") or "")

            header_parts: list[str] = []
            if source:
                header_parts.append(source)
            if title:
                header_parts.append(title)
            header_text = " · ".join(header_parts)

            # Truncate body if the full plain-text rendering would exceed
            # terminal width. Compute against the visible (un-styled) form
            # so ANSI escapes don't throw off the math.
            visible_prefix = f"  ▎ {icon}  {header_text}"
            sep = "  —  " if (body and header_text) else ("  " if body else "")
            visible_full = visible_prefix + sep + body
            if len(visible_full) > self._width - 1:
                budget = max(0, self._width - 1 - len(visible_prefix) - len(sep))
                if budget < 4:
                    body = ""
                    sep = ""
                else:
                    body = body[: max(0, budget - 3)] + "..."

            bar = _styled("  ▎ ", STYLE_DIM)
            icon_str = _styled(f"{icon}  ", icon_style)
            header_str = _styled(header_text, icon_style) if header_text else ""
            sep_str = _styled(sep, STYLE_DIM) if sep else ""
            body_str = _styled(body, STYLE_DIM) if body else ""
            lines.append(bar + icon_str + header_str + sep_str + body_str)
        return lines

    def _render_banners(self) -> list[str]:
        """Render sticky banners above the notification stack."""
        if not self._banners:
            return []
        lines: list[str] = []
        for banner in self._banners.values():
            kind = getattr(banner, "kind", "")
            kind_str = kind.value if isinstance(kind, Enum) else str(kind)
            title = getattr(banner, "title", "") or ""
            body = getattr(banner, "body", "") or ""
            actions = getattr(banner, "actions", ())
            # Banner styled by kind: warn for compaction/plan, error for
            # arbiter_kill / capability_denial.
            severity_style = (
                STYLE_ERROR
                if kind_str in {"arbiter_kill", "capability_denial"}
                else STYLE_WARN
            )
            header = f"  ⚡ {title}"
            if header.strip():
                lines.append(_styled(_sanitize(header), severity_style))
            if body:
                body_line = f"     {body}"
                if len(body_line) > self._width - 1:
                    body_line = body_line[: max(0, self._width - 4)] + "..."
                lines.append(_styled(_sanitize(body_line), STYLE_DIM))
            if actions:
                action_line = "     Actions: " + ", ".join(actions)
                lines.append(_styled(_sanitize(action_line), STYLE_DIM))
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
        lines.append(_styled(bl + h_char * (w - 2) + br, border_s))
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
            logger.debug("suppressed exception in _ensure_frame_task", exc_info=True)

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
            logger.debug("suppressed exception in _frame_loop", exc_info=True)

    def _render_frame(self) -> None:
        """Synchronous single-frame render (used by finish)."""
        self._draw_live_region()

    # ── I/O helpers ───────────────────────────────────────────────────────

    def _write(self, s: str) -> None:
        try:
            self._out.write(s)
            self._out.flush()
        except Exception:
            logger.debug("suppressed exception in _write", exc_info=True)

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
            logger.debug("suppressed exception in _update_status", exc_info=True)

    # ── Tool registry (lazy) ──────────────────────────────────────────────

    def _get_tool_registry(self) -> Any:
        if self._tool_registry is None:
            from obscura.cli.renderer.modern.tool_renderers import ToolRendererRegistry

            self._tool_registry = ToolRendererRegistry()
        return self._tool_registry
