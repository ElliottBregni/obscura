"""obscura.cli.tui.renderer — TUIState-mutating renderer.

Implements :class:`obscura.cli.renderer.protocol.RendererProtocol` against
a :class:`obscura.cli.tui.state.TUIState`. Where the legacy
:class:`obscura.cli.render.StreamRenderer` writes Markdown blocks to
stdout, this renderer mutates the typed state container in place; the
prompt-toolkit ``Application`` re-reads state on every frame and an
optional ``invalidate`` callback nudges it when something changed.

Design notes
------------
* Every incoming :class:`~obscura.core.types.AgentEvent` is funnelled
  through :func:`obscura.cli.renderer.channels.from_agent_event`. The
  resulting union is dispatched per-channel: transcript entries get
  appended to ``state.transcript``; status events mutate ``state.live``;
  notifications append to ``state.notifications``; banners replace
  ``state.banner``.
* Per-turn buffering mirrors the legacy renderer:

    - ``TEXT_DELTA`` (:attr:`AgentEventKind.TEXT_DELTA`) appends to
      ``self._text_buf``; flushed as one ASSISTANT entry on
      :attr:`AgentEventKind.TURN_COMPLETE` /
      :attr:`AgentEventKind.AGENT_DONE`.
    - ``THINKING_DELTA`` (:attr:`AgentEventKind.THINKING_DELTA`) appends
      to ``self._thinking_buf``; flushed as one THINKING entry as soon
      as a non-thinking event arrives.
    - ``TOOL_CALL`` (:attr:`AgentEventKind.TOOL_CALL`) and
      ``TOOL_RESULT`` (:attr:`AgentEventKind.TOOL_RESULT`) emit one
      entry each immediately.
    - ``ERROR`` (:attr:`AgentEventKind.ERROR`) flushes any pending
      buffers, then emits an ERROR entry.

* The renderer is pure(ish): it mutates only the injected ``TUIState``
  (and its own private buffers) and calls ``invalidate``. There is no
  module-level mutable state.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from io import StringIO
from typing import Any

from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from rich.console import Console as RichConsole
from rich.markdown import Markdown as RichMarkdown

from obscura.cli.renderer.channels import (
    Banner,
    BannerKind,
    Notification,
    Severity,
    StatusEvent,
    TranscriptEvent,
    from_agent_event,
)
from obscura.cli.tool_summaries import classify_tool, summarize_tool_call
from obscura.cli.tui.formatter import format_slash_output
from obscura.cli.tui.state import (
    BannerState,
    LiveRegionKind,
    NotificationItem,
    StyledRun,
    TUIState,
    TranscriptEntry,
    TranscriptKind,
)
from obscura.core.enums.agent import AgentEventKind
from obscura.core.types import AgentEvent

logger = logging.getLogger(__name__)

__all__ = ["TUIRenderer"]


# ---------------------------------------------------------------------------
# Style tokens
# ---------------------------------------------------------------------------
# Kept in one place so the formatter (when wired in later) can reuse the
# same class names. Values follow prompt-toolkit's ``class:foo`` syntax.

_STYLE_USER = "class:transcript.user"
_STYLE_ASSISTANT = "class:transcript.assistant"
_STYLE_THINKING = "class:transcript.thinking"
_STYLE_TOOL_CALL = "class:transcript.tool_call"
_STYLE_TOOL_RESULT = "class:transcript.tool_result"
_STYLE_TOOL_RESULT_ERR = "class:transcript.tool_result.error"
_STYLE_SYSTEM = "class:transcript.system"
_STYLE_ERROR = "class:transcript.error"
_STYLE_SLASH = "class:transcript.slash"

# Hex used by ``_format_tool_result_runs`` for the ``… (N more lines)``
# truncation hint. Pulled inline rather than imported from theme so
# the formatter is callable from tests without touching the theme
# module's lazy imports.
_OVERLAY_FOR_TRUNCATION = "#6c7086"


# ---------------------------------------------------------------------------
# Banner-kind ↔ TUI banner-kind table
# ---------------------------------------------------------------------------

_BANNER_KIND_MAP: dict[BannerKind, str] = {
    BannerKind.PLAN_APPROVAL: "plan_approval",
    BannerKind.CAPABILITY_DENIAL: "capability_denial",
    BannerKind.ARBITER_KILL: "arbiter_kill",
    BannerKind.COMPACTION: "compaction",
}


# Marker emitted by :func:`obscura.core.tool_bridge.maybe_truncate_result`
# when a tool's output exceeds 200 KB. We pull the cached path out so
# the TUI can show a toast pointing at it.
_OVERFLOW_MARKER_RE = re.compile(
    r"\[Result truncated[^\]]*Full result saved to:\s*([^\]]+?)\]",
)


def _extract_overflow_path(text: str) -> str:
    """Return the cached-result file path from a truncation marker, or ""."""
    if "[Result truncated" not in text:
        return ""
    match = _OVERFLOW_MARKER_RE.search(text)
    if match is None:
        return ""
    return match.group(1).strip()


# ANSI escape sequence stripper — same shape as the modern renderer's
# ``_sanitize`` helper. Tool output coming from shells / MCP servers
# may include colour codes that prompt-toolkit will treat as literal
# characters in the transcript window; strip them before display.
_ANSI_CSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]+")


def _sanitize(text: str) -> str:
    """Strip ANSI escapes and control characters from ``text``."""
    if not text:
        return ""
    cleaned = _ANSI_CSI_RE.sub("", text)
    cleaned = _ANSI_OSC_RE.sub("", cleaned)
    cleaned = re.sub(r"\x1B[@-Z\\-_]", "", cleaned)
    cleaned = re.sub(r"\x1B", "", cleaned)
    return _CONTROL_CHARS_RE.sub("", cleaned)


# Default cap for tool result lines in the transcript. Mirrors the
# modern renderer's ``OBSCURA_TOOL_OUTPUT_MAX_LINES`` so behaviour
# stays consistent across the two surfaces.
_DEFAULT_TOOL_OUTPUT_MAX_LINES = 80


def _tool_output_line_cap() -> int:
    raw = os.environ.get("OBSCURA_TOOL_OUTPUT_MAX_LINES", "")
    if not raw:
        return _DEFAULT_TOOL_OUTPUT_MAX_LINES
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_TOOL_OUTPUT_MAX_LINES


def _try_parse_json(raw: str) -> Any | None:
    """Parse ``raw`` as JSON, or return ``None`` if it isn't JSON.

    Tool results from MCP / system tools commonly return a JSON-encoded
    dict like ``{"ok": true, "stdout": "...", "stderr": ""}``. Showing
    that verbatim in the transcript creates the unreadable
    ``↳ {"ok": true, …}`` lines the user complained about. Parsing it
    here lets the formatter pull out the readable parts (``stdout``,
    ``message``, ``result``, etc.) and drop the structural noise.
    """
    raw = raw.strip()
    if not raw or raw[0] not in "{[":
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


# Keys that carry the human-readable payload of a JSON tool result.
# Order matters: a successful shell call's body lives in ``stdout``;
# a failed call's body lives in ``stderr``. Generic tools use
# ``output`` / ``message`` / ``result`` / ``content``. We pick the
# first key with a non-empty string value.
_TOOL_RESULT_BODY_KEYS: tuple[str, ...] = (
    "stdout",
    "stderr",
    "output",
    "message",
    "result",
    "content",
    "text",
    "value",
)


def _summarize_json_result(parsed: Any) -> str:
    """Render a parsed JSON tool result as a short human-readable string.

    Falls back to a one-line ``key=value`` summary for small dicts and
    a single ``str(parsed)`` for anything else. Never raises.
    """
    if isinstance(parsed, str):
        return parsed
    if isinstance(parsed, list):
        items: list[Any] = parsed  # type: ignore[reportUnknownVariableType]
        return f"({len(items)} items)"
    if not isinstance(parsed, dict):
        return str(parsed)

    parsed_dict: dict[str, Any] = parsed  # type: ignore[reportUnknownVariableType]

    # Prefer a recognised body field — that's the human-readable part.
    for key in _TOOL_RESULT_BODY_KEYS:
        value: Any = parsed_dict.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (int, float, bool)) and key not in ("ok",):
            return f"{key}: {value}"

    # No recognised body field — show a short ``k: v`` summary of the
    # interesting (non-status) keys, preserving order. Skips noise
    # like ``ok`` / ``status`` so the line surfaces actual data.
    skip = {"ok", "status", "code", "exit_code"}
    parts: list[str] = []
    for k, v in parsed_dict.items():
        if k in skip:
            continue
        text = str(v)
        if len(text) > 60:
            text = text[:57] + "…"
        parts.append(f"{k}: {text}")
        if len(parts) >= 4:
            break
    if parts:
        return " · ".join(parts)
    # Pure-status dict like ``{"ok": true}``: keep the OK / fail signal.
    if "ok" in parsed_dict:
        return "ok" if parsed_dict["ok"] else "failed"
    return ""


def _format_tool_result_runs(
    event: AgentEvent,
    *,
    error_style: str,
    success_style: str,
    detail_style: str,
    muted_style: str,
) -> list[StyledRun]:
    """Convert ``event.tool_result`` into a styled multi-line list.

    Mirrors the modern renderer's ``_handle_tool_result``: sanitise
    ANSI, JSON-decode dict-shaped output, line-split, cap line count,
    and indent continuation lines. The first row gets a ``✓``/``✗``
    severity glyph; subsequent rows align under it.
    """
    raw = (event.tool_result or "").rstrip()
    is_error = bool(event.is_error)

    # Strip ANSI before any further parsing — colours from a shell
    # tool would otherwise survive into the transcript as literal
    # escape sequences.
    cleaned = _sanitize(raw)

    # If the result is a JSON dict, surface the human-readable body
    # (stdout / message / result / etc.) so the transcript shows
    # something a reader can scan at a glance instead of the literal
    # ``{"ok": true, "stdout": "…"}`` envelope.
    parsed: Any = _try_parse_json(cleaned)
    if parsed is not None:
        summary = _summarize_json_result(parsed)
        if summary:
            cleaned = summary
            # When the dict carries an explicit failure signal, prefer
            # the error style even if ``event.is_error`` was unset.
            if isinstance(parsed, dict) and not is_error:
                parsed_d: dict[str, Any] = parsed  # type: ignore[reportUnknownVariableType]
                exit_code: Any = parsed_d.get("exit_code")
                if (
                    parsed_d.get("ok") is False
                    or (isinstance(exit_code, int) and exit_code != 0)
                    or parsed_d.get("status") in ("error", "failed")
                ):
                    is_error = True
        elif isinstance(parsed, dict) and not parsed:
            cleaned = "(empty result)"

    if not cleaned.strip():
        cleaned = "(empty result)"

    glyph_style = error_style if is_error else success_style
    body_style = error_style if is_error else detail_style
    glyph = "✗" if is_error else "✓"

    lines = cleaned.split("\n")
    cap = _tool_output_line_cap()
    truncated = len(lines) > cap
    if truncated:
        lines = lines[:cap]

    runs: list[StyledRun] = [
        StyledRun(text=f"{glyph} ", style=f"{glyph_style} bold"),
        StyledRun(text=lines[0], style=body_style),
    ]
    for ln in lines[1:]:
        runs.append(StyledRun(text="\n    "))
        runs.append(StyledRun(text=ln, style=body_style))
    if truncated:
        all_lines = cleaned.split("\n")
        more = len(all_lines) - cap
        runs.append(StyledRun(text="\n    "))
        runs.append(
            StyledRun(
                text=f"… ({more} more lines)",
                style=muted_style,
            ),
        )
    return runs


_MARKDOWN_RENDER_WIDTH = 100
_MARKDOWN_CODE_THEME = "monokai"


def _markdown_to_runs(text: str, *, fallback_style: str = "") -> list[StyledRun]:
    """Render ``text`` as Rich Markdown and convert to prompt-toolkit runs.

    Rich emits ANSI escape sequences; prompt_toolkit's :class:`ANSI` class
    parses those into ``(style, text)`` fragments which map cleanly onto
    :class:`StyledRun`. If anything raises, falls back to a single
    plain-text run carrying ``fallback_style`` so the message still shows.
    """
    try:
        buf = StringIO()
        rich_console = RichConsole(
            file=buf,
            force_terminal=True,
            color_system="truecolor",
            width=_MARKDOWN_RENDER_WIDTH,
            legacy_windows=False,
            highlight=False,
            soft_wrap=False,
        )
        rich_console.print(RichMarkdown(text, code_theme=_MARKDOWN_CODE_THEME))
        rendered = buf.getvalue()
        if not rendered:
            return [StyledRun(text=text, style=fallback_style)]
        fragments = to_formatted_text(ANSI(rendered))
        runs: list[StyledRun] = []
        for frag in fragments:
            # Fragments are (style, text) or (style, text, mouse_handler).
            style = str(frag[0]) if len(frag) > 0 else ""
            run_text = str(frag[1]) if len(frag) > 1 else ""
            if not run_text:
                continue
            runs.append(StyledRun(text=run_text, style=style))
        return runs
    except Exception:
        logger.debug("markdown render failed; falling back to plain", exc_info=True)
        return [StyledRun(text=text, style=fallback_style)]


class TUIRenderer:
    """Implements :class:`RendererProtocol` against a :class:`TUIState`.

    Every agent event is routed through
    :func:`obscura.cli.renderer.channels.from_agent_event`, then dispatched
    to a per-channel handler that mutates the ``TUIState``. No stdout
    writes — the prompt-toolkit ``Application`` renders from state on
    every frame.

    Parameters
    ----------
    state:
        The mutable state container to update.
    invalidate:
        Optional zero-arg callback invoked after every event so the
        owning prompt-toolkit ``Application`` can request a redraw.
        ``None`` is safe (used by tests).
    """

    def __init__(
        self,
        state: TUIState,
        *,
        invalidate: Callable[[], None] | None = None,
    ) -> None:
        self._state: TUIState = state
        self._invalidate: Callable[[], None] | None = invalidate

        # Per-turn accumulators — mirror StreamRenderer.
        self._text_buf: list[str] = []
        self._thinking_buf: list[str] = []
        self._all_text: list[str] = []
        self._thinking_blocks: list[str] = []
        self._in_thinking: bool = False
        # Live transcript entry for the current thinking block. Created on
        # the first THINKING_DELTA and updated in place each subsequent delta
        # so thinking streams into the transcript as it arrives rather than
        # appearing all at once when the block ends.
        self._thinking_entry: TranscriptEntry | None = None

        # Reveal-aware flush queue — same shape as the bordered REPL.
        # Events whose handler commits ``_text_buf`` to the transcript
        # are deferred until ``state.live.reveal_pos`` has caught up to
        # ``state.live.full_text`` so the committed transcript entry
        # matches what the user just watched type in. Drained from
        # ``ObscuraTUIApp._reveal_tick`` after each frame's reveal
        # advance. Hard timeout per entry caps the wait.
        self._pending_events: list[tuple[AgentEvent, float]] = []
        self._FLUSH_WAIT_TIMEOUT_S: float = 1.5

    # ------------------------------------------------------------------
    # RendererProtocol surface
    # ------------------------------------------------------------------

    # Event kinds whose handler unconditionally commits ``_text_buf``
    # to the transcript via :meth:`_flush_text`. Deferred until the
    # reveal cursor has caught up so the committed entry matches the
    # text the user just watched type in. THINKING_DELTA is
    # conditional — see :meth:`_event_will_flush_text`.
    _UNCONDITIONAL_FLUSH_KINDS: frozenset[AgentEventKind] = frozenset(
        {
            AgentEventKind.TOOL_CALL,
            AgentEventKind.TURN_COMPLETE,
            AgentEventKind.AGENT_DONE,
            AgentEventKind.ERROR,
        },
    )

    # Terminal events — no more text is coming. Skip the deferral
    # queue and force-drain pending events with an immediate snap
    # forward so the transcript is consistent before the run ends.
    _TERMINAL_KINDS: frozenset[AgentEventKind] = frozenset(
        {AgentEventKind.AGENT_DONE, AgentEventKind.ERROR},
    )

    def _event_will_flush_text(self, event: AgentEvent) -> bool:
        """Will dispatching ``event`` commit ``_text_buf`` to the transcript?

        Used to decide whether to defer the event until the reveal
        cursor has caught up. ``THINKING_DELTA`` only flushes on the
        first delta after text mode (the text → thinking transition);
        subsequent thinking deltas just append to ``_thinking_buf``.
        """
        if event.kind in self._UNCONDITIONAL_FLUSH_KINDS:
            return True
        if event.kind == AgentEventKind.THINKING_DELTA and not self._in_thinking:
            return bool(self._text_buf)
        return False

    def handle(self, event: AgentEvent) -> None:
        """Route a single :class:`AgentEvent` to the appropriate channel.

        See :func:`obscura.cli.renderer.channels.from_agent_event` for the
        routing rules. After dispatch, expired notifications are pruned
        and the optional ``invalidate`` callback is fired so the owning
        ``Application`` can redraw.
        """
        # Terminal events — no more text is coming. Snap the reveal,
        # drain the queue, then dispatch this event. Without this,
        # a queued AGENT_DONE / ERROR would sit forever in test
        # contexts that don't run the app's ``_reveal_tick``.
        if event.kind in self._TERMINAL_KINDS:
            self._force_drain_pending()
            self._dispatch_event(event)
            return

        # Preserve order: once anything is queued, everything queues
        # behind it until the drain runs. Otherwise a TEXT_DELTA could
        # slip past a deferred TOOL_CALL and end up rendered after
        # the tool-call transcript entry.
        #
        # No invalidate on the queue path — :meth:`drain_pending_events`
        # fires it (via ``_dispatch_event``) once each event actually
        # commits, so we get exactly one invalidate per event regardless
        # of how long it sat queued. The reveal-tick keeps the live
        # preview animating in the meantime.
        if self._pending_events:
            self._pending_events.append(
                (event, time.monotonic() + self._FLUSH_WAIT_TIMEOUT_S),
            )
            return

        # Defer flush-triggering events when the reveal cursor is
        # still chasing the live buffer — drained by
        # :meth:`drain_pending_events` from the app's ``_reveal_tick``.
        if self._event_will_flush_text(event):
            backlog = len(self._state.live.full_text) - self._state.live.reveal_pos
            if backlog > 0:
                self._pending_events.append(
                    (event, time.monotonic() + self._FLUSH_WAIT_TIMEOUT_S),
                )
                return

        self._dispatch_event(event)

    def _force_drain_pending(self) -> None:
        """Snap reveal forward and replay every queued event in order.

        Used by terminal events and :meth:`finish` to flush the
        backlog when waiting any longer would lose information.
        """
        if not self._pending_events:
            return
        live = self._state.live
        live.reveal_pos = len(live.full_text)
        live.preview = live.full_text
        for event, _deadline in list(self._pending_events):
            self._dispatch_event(event)
        self._pending_events.clear()

    def _dispatch_event(self, event: AgentEvent) -> None:
        """Execute the renderer-side action for ``event`` immediately.

        Split out from :meth:`handle` so the flush-deferral queue can
        replay queued events without re-entering the queueing logic.
        """
        rendered = from_agent_event(event)
        if isinstance(rendered, TranscriptEvent):
            self._handle_transcript(rendered.event)
        elif isinstance(rendered, StatusEvent):
            self._handle_status(rendered)
        elif isinstance(rendered, Notification):
            self._handle_notification(rendered)
        elif isinstance(rendered, Banner):  # pyright: ignore[reportUnnecessaryIsInstance]
            self._handle_banner(rendered)

        # Cheap, idempotent — drop expired toasts on every tick.
        self._state.prune_notifications()
        self._fire_invalidate()

    def drain_pending_events(self) -> int:
        """Replay queued events whose flush is now safe to commit.

        Called by the app's ``_reveal_tick`` after advancing the
        reveal cursor. Drains while the head event either does not
        require a flush or its required reveal has caught up. On
        per-event timeout, snaps the reveal forward so the impending
        commit matches what the user is about to see this frame.

        Returns the number of events drained (callers may use this
        to decide whether to invalidate).
        """
        if not self._pending_events:
            return 0
        drained = 0
        now = time.monotonic()
        live = self._state.live
        while self._pending_events:
            event, deadline = self._pending_events[0]
            if self._event_will_flush_text(event):
                full_len = len(live.full_text)
                backlog = full_len - live.reveal_pos
                if backlog > 0:
                    if now < deadline:
                        # Reveal still chasing — wait for next frame.
                        break
                    # Timeout: snap forward so the impending commit
                    # matches what the user will see on this frame.
                    live.reveal_pos = full_len
                    live.preview = live.full_text
            self._pending_events.pop(0)
            self._dispatch_event(event)
            drained += 1
            now = time.monotonic()
        return drained

    def finish(self) -> None:
        """Flush any pending buffers and reset the live region.

        Called at the end of a turn (and again on cancellation). Safe to
        call repeatedly — flushing an empty buffer is a no-op.
        """
        # Drain any events still waiting on the reveal cursor — at
        # turn end / cancellation there's nothing left to type, replay
        # them so we don't lose tool-call entries / errors that were
        # queued.
        self._force_drain_pending()
        self._flush_thinking()
        self._flush_text()
        self._state.live.reset()
        self._state.hud.is_streaming = False
        self._fire_invalidate()

    def get_accumulated_text(self) -> str:
        """Return all accumulated assistant text for this turn.

        Mirrors :meth:`StreamRenderer.get_accumulated_text`: returns
        every TEXT_DELTA seen so far this turn, plus any unflushed
        thinking buffer contents (so the agent loop can capture
        partial output even if the turn was cancelled).

        ``_all_text`` already contains everything ``_text_buf`` does —
        both are appended on every TEXT_DELTA — so concatenating both
        would double-count the unflushed segment. Pre-flush this
        difference was invisible because the immediate dispatch always
        cleared ``_text_buf`` before this method ran; the reveal-aware
        flush queue can leave ``_text_buf`` populated, exposing the
        latent double-count.
        """
        parts: list[str] = []
        parts.extend(self._all_text)
        parts.extend(self._thinking_buf)
        return "".join(parts)

    def get_thinking_blocks(self) -> list[str]:
        """Return completed thinking/reasoning blocks (not the live buffer)."""
        return list(self._thinking_blocks)

    def get_last_thinking(self) -> str:
        """Return the most recent finished thinking block, or ``""``."""
        return self._thinking_blocks[-1] if self._thinking_blocks else ""

    # ------------------------------------------------------------------
    # TUI-specific helpers (also called from overlays)
    # ------------------------------------------------------------------

    def push_user_prompt(self, text: str) -> None:
        """Append a USER transcript entry for ``text``.

        Used by the input box once the user submits a prompt — the agent
        loop receives the same text but doesn't re-emit it as an event.
        """
        if not text:
            return
        entry = TranscriptEntry(
            kind=TranscriptKind.USER,
            runs=[StyledRun(text=text, style=_STYLE_USER)],
        )
        self._state.append_transcript(entry)
        self._fire_invalidate()

    def push_slash_output(self, captured_rich_text: str) -> None:
        """Append a SLASH_OUTPUT transcript entry.

        Slash commands render via Rich into a string; this delegates to
        :func:`obscura.cli.tui.formatter.format_slash_output` for ANSI
        handling.
        """
        if not captured_rich_text:
            return
        # delegates to formatter for ANSI handling
        entry = format_slash_output(captured_rich_text)
        self._state.append_transcript(entry)
        self._fire_invalidate()

    def push_system_message(self, text: str) -> None:
        """Append a SYSTEM transcript entry — banner commits, /clear notices, etc."""
        if not text:
            return
        entry = TranscriptEntry(
            kind=TranscriptKind.SYSTEM,
            runs=[StyledRun(text=text, style=_STYLE_SYSTEM)],
        )
        self._state.append_transcript(entry)
        self._fire_invalidate()

    # ------------------------------------------------------------------
    # Channel dispatch
    # ------------------------------------------------------------------

    def _handle_transcript(self, event: AgentEvent) -> None:
        """Dispatch a transcript-bound :class:`AgentEvent` by ``kind``."""
        kind = event.kind

        if kind == AgentEventKind.TURN_START:
            # Begin a fresh turn — clamp leftover state. ``finish`` already
            # ran on the previous turn, but a defensive reset costs nothing.
            self._in_thinking = False
            self._thinking_entry = None
            self._state.live.kind = LiveRegionKind.THINKING
            self._state.live.label = "thinking"
            self._state.live.preview = ""
            self._state.live.started_at_monotonic = time.monotonic()
            self._state.hud.is_streaming = True
            return

        if kind == AgentEventKind.THINKING_DELTA:
            # First thinking delta of the block flushes any pending text
            # so the THINKING entry lands above subsequent ASSISTANT text.
            if not self._in_thinking:
                self._flush_text()
                self._in_thinking = True
                # Create a live transcript entry immediately so thinking
                # streams delta-by-delta into the transcript rather than
                # appearing all at once when the block ends.
                entry = TranscriptEntry(
                    kind=TranscriptKind.THINKING,
                    runs=[StyledRun(text="", style=_STYLE_THINKING)],
                )
                self._thinking_entry = entry
                self._state.append_transcript(entry)
            self._thinking_buf.append(event.text)
            # Update the live entry's runs in place — TranscriptEntry is
            # not frozen so this is safe; StyledRun is frozen so we replace
            # the list rather than mutating a run.
            if self._thinking_entry is not None:
                self._thinking_entry.runs = [
                    StyledRun(
                        text="".join(self._thinking_buf),
                        style=_STYLE_THINKING,
                    )
                ]
            self._update_live_thinking()
            return

        if kind == AgentEventKind.TEXT_DELTA:
            if self._in_thinking:
                self._flush_thinking()
            self._text_buf.append(event.text)
            self._all_text.append(event.text)
            self._update_live_streaming()
            return

        if kind == AgentEventKind.TOOL_CALL:
            self._flush_thinking()
            self._flush_text()
            self._emit_tool_call(event)
            return

        if kind == AgentEventKind.TOOL_RESULT:
            self._emit_tool_result(event)
            return

        if kind in (AgentEventKind.TURN_COMPLETE, AgentEventKind.AGENT_DONE):
            self._flush_thinking()
            self._flush_text()
            self._state.live.reset()
            self._state.hud.is_streaming = False
            return

        if kind == AgentEventKind.ERROR:
            self._flush_thinking()
            self._flush_text()
            self._emit_error(event.text or "")
            self._state.live.reset()
            self._state.hud.is_streaming = False
            return

        # Other transcript-routed kinds (USER_INPUT, AGENT_START, etc.)
        # carry no display semantics here — the formatter can grow
        # branches later. For now, ignore silently to keep the renderer
        # forward-compatible.

    def _handle_status(self, ev: StatusEvent) -> None:
        """Apply a :class:`StatusEvent` to ``state.live``.

        ``active=False`` calls reset() on the live region (mirroring the
        legacy ``_stop_status``).
        """
        live = self._state.live
        if not ev.active:
            live.reset()
            return
        # Transitioning from IDLE → active resets the start timestamp so
        # ``elapsed_s`` reflects only the current activity span.
        if live.kind == LiveRegionKind.IDLE:
            live.started_at_monotonic = time.monotonic()
        # Without a richer signal from StatusEvent, treat it as a generic
        # streaming indicator. The agent-event handlers above set the
        # more specific kind values.
        live.kind = LiveRegionKind.STREAMING
        live.label = ev.text
        live.preview = ev.preview

    def _handle_notification(self, n: Notification) -> None:
        """Convert a channel :class:`Notification` to a :class:`NotificationItem`."""
        item = NotificationItem(
            title=n.title,
            body=n.body,
            severity=n.severity,
            source=n.source,
            key=n.key,
            ttl_seconds=n.ttl_seconds,
        )
        self._state.push_notification(item)

    def _handle_banner(self, b: Banner) -> None:
        """Replace ``state.banner`` with the given banner."""
        kind = _BANNER_KIND_MAP.get(b.kind, "compaction")
        self._state.banner = BannerState(
            kind=kind,  # type: ignore[arg-type]  # BannerState uses Literal
            title=b.title,
            body=b.body,
            actions=list(b.actions),
        )

    # ------------------------------------------------------------------
    # Buffer flush helpers
    # ------------------------------------------------------------------

    def _flush_text(self) -> None:
        """Flush ``_text_buf`` as a single ASSISTANT transcript entry.

        The accumulated text is run through Rich's Markdown renderer so
        headers, bold/italic, lists, inline code, and syntax-highlighted
        code blocks land as styled runs in the transcript instead of a
        single flat string.
        """
        if not self._text_buf:
            return
        text = "".join(self._text_buf)
        self._text_buf.clear()
        if not text.strip():
            return
        runs = _markdown_to_runs(text, fallback_style=_STYLE_ASSISTANT)
        if not runs:
            runs = [StyledRun(text=text, style=_STYLE_ASSISTANT)]
        entry = TranscriptEntry(
            kind=TranscriptKind.ASSISTANT,
            runs=runs,
        )
        self._state.append_transcript(entry)

    def _flush_thinking(self) -> None:
        """Finalise the live THINKING transcript entry and reset thinking state.

        The transcript entry was created on the first THINKING_DELTA and
        updated in place on each subsequent delta, so there is nothing left
        to append here. We just record the completed block in
        ``_thinking_blocks`` for context-window compaction and clear all
        accumulators.
        """
        if not self._thinking_buf:
            self._in_thinking = False
            self._thinking_entry = None
            return
        text = "".join(self._thinking_buf)
        self._thinking_buf.clear()
        self._in_thinking = False
        self._thinking_entry = None
        if not text.strip():
            return
        self._thinking_blocks.append(text)
        # Entry already committed live to the transcript during streaming —
        # do not append a second copy.

    # ------------------------------------------------------------------
    # Tool / error emission
    # ------------------------------------------------------------------

    def _emit_tool_call(self, event: AgentEvent) -> None:
        """Emit a TOOL_USE transcript entry from a TOOL_CALL event."""
        name = event.tool_name or "tool"
        # Use the shared summariser so the TUI matches the bordered
        # REPL exactly: ``read_text_file {"path": "x.py"}`` →
        # ``Reading x.py``; ``run_command {"command": "ls"}`` →
        # ``$ ls``; MCP shadows render as ``server.tool — arg``.
        try:
            summary = summarize_tool_call(name, dict(event.tool_input or {}))
            kind = classify_tool(name)
        except Exception:
            logger.debug("suppressed exception in _emit_tool_call", exc_info=True)
            summary = ""
            kind = "native"

        # Per-kind tag mirrors the modern renderer's ``_TOOL_KIND_STYLES``
        # so MCP / shell / plugin / delegation tools are scannable at a
        # glance even on a busy transcript.
        kind_tag = {
            "shell": "$",
            "mcp": "MCP",
            "plugin": "PLUG",
            "delegation": "TASK",
        }.get(kind, "")

        runs: list[StyledRun] = []
        if kind_tag:
            runs.append(
                StyledRun(text=f"{kind_tag} ", style=f"{_STYLE_TOOL_CALL} italic"),
            )
        runs.append(StyledRun(text=name, style=f"{_STYLE_TOOL_CALL} bold"))
        if summary:
            runs.append(StyledRun(text="  "))
            runs.append(StyledRun(text=summary, style=_STYLE_TOOL_CALL))
        entry = TranscriptEntry(
            kind=TranscriptKind.TOOL_USE,
            runs=runs,
            metadata={
                "tool_name": name,
                "tool_use_id": event.tool_use_id,
                "tool_input": dict(event.tool_input),
            },
        )
        self._state.append_transcript(entry)

        live = self._state.live
        live.kind = LiveRegionKind.TOOL_RUNNING
        live.label = f"running {name}"
        # Cap the live preview at ~50 chars so it never wraps the
        # single-row live region on narrow terminals. The truncation
        # in :func:`live_region_text` is a backstop, but trimming here
        # also keeps ``state.live.preview`` lean for callers that
        # render it elsewhere.
        live.preview = summary[:50] if summary else ""
        live.started_at_monotonic = time.monotonic()

    def _emit_tool_result(self, event: AgentEvent) -> None:
        """Emit a TOOL_RESULT transcript entry from a TOOL_RESULT event."""
        raw = (event.tool_result or "").rstrip()
        # Build the styled body via the shared formatter — sanitises
        # ANSI, decodes JSON envelopes (``{"ok": true, "stdout": …}``),
        # splits multi-line output, and caps at OBSCURA_TOOL_OUTPUT_MAX_LINES.
        runs = _format_tool_result_runs(
            event,
            error_style=_STYLE_TOOL_RESULT_ERR,
            success_style=_STYLE_TOOL_RESULT,
            detail_style=_STYLE_TOOL_RESULT,
            muted_style=f"fg:{_OVERLAY_FOR_TRUNCATION}",
        )
        entry = TranscriptEntry(
            kind=TranscriptKind.TOOL_RESULT,
            runs=runs,
            metadata={
                "tool_name": event.tool_name,
                "tool_use_id": event.tool_use_id,
                "is_error": event.is_error,
            },
        )
        self._state.append_transcript(entry)

        # Truncation marker — surface the cached file path as a toast
        # AND stash it on the state so a future ``/last-output``
        # palette entry can pop it open. Doing both is intentional: the
        # toast is the "you should know" signal the user can ignore;
        # the stash is the "I want to look now" affordance.
        overflow_path = _extract_overflow_path(raw)
        if overflow_path:
            self._state.last_overflow_path = overflow_path
            self._state.last_overflow_tool = event.tool_name or ""
            self._state.push_notification(
                NotificationItem(
                    title="Output cached",
                    body=(
                        f"{event.tool_name or 'tool'} result was large; "
                        f"full text saved to {overflow_path}"
                    ),
                    severity=Severity.INFO,
                    source="tui",
                    key=f"tui.overflow.{event.tool_use_id}",
                    ttl_seconds=12.0,
                ),
            )

        # Returning to a streaming-or-idle state is the agent loop's job;
        # most backends emit the next TEXT_DELTA / TURN_COMPLETE shortly
        # after a tool result, so we just clear the tool-running label.
        if self._state.live.kind == LiveRegionKind.TOOL_RUNNING:
            self._state.live.kind = LiveRegionKind.STREAMING
            self._state.live.label = ""
            self._state.live.preview = ""

    def _emit_error(self, text: str) -> None:
        """Emit an ERROR transcript entry."""
        entry = TranscriptEntry(
            kind=TranscriptKind.ERROR,
            runs=[StyledRun(text=text or "error", style=_STYLE_ERROR)],
        )
        self._state.append_transcript(entry)

    # ------------------------------------------------------------------
    # Live-region preview helpers
    # ------------------------------------------------------------------

    def _update_live_thinking(self) -> None:
        """Refresh ``state.live`` for thinking deltas.

        Seeds the full thinking text into ``live.full_text``; the
        reveal-cursor tick (``app._reveal_tick``) advances the visible
        ``preview`` along it with jittered bursts so the preview reads
        as organic typing instead of snapping per-delta.
        """
        live = self._state.live
        if live.kind == LiveRegionKind.IDLE:
            live.started_at_monotonic = time.monotonic()
            live.reveal_pos = 0
        if live.kind != LiveRegionKind.THINKING:
            # Clamp: never let reveal_pos jump backwards on a kind
            # transition (e.g. STREAMING → THINKING mid-turn).
            live.reveal_pos = max(live.reveal_pos, 0)
        live.kind = LiveRegionKind.THINKING
        live.label = "thinking"
        live.full_text = "".join(self._thinking_buf)

    def _update_live_streaming(self) -> None:
        """Refresh ``state.live`` for assistant text deltas.

        Seeds the full streamed text into ``live.full_text``; the
        reveal-cursor tick advances ``preview`` along it (see
        :func:`_update_live_thinking`).
        """
        live = self._state.live
        if live.kind == LiveRegionKind.IDLE:
            live.started_at_monotonic = time.monotonic()
            live.reveal_pos = 0
        if live.kind != LiveRegionKind.STREAMING:
            # Clamp: never let reveal_pos jump backwards on a kind
            # transition (e.g. THINKING → STREAMING mid-turn).
            live.reveal_pos = max(live.reveal_pos, 0)
        live.kind = LiveRegionKind.STREAMING
        live.label = "streaming"
        live.full_text = "".join(self._text_buf)

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def _fire_invalidate(self) -> None:
        """Best-effort invalidate — swallow callback errors so renderer never raises."""
        cb = self._invalidate
        if cb is None:
            return
        try:
            cb()
        except Exception:
            # The Application owns the callback; if it raises mid-frame
            # the renderer must not propagate or it would corrupt the
            # event loop. Logged at debug so deep logs still surface
            # the misbehaving invalidate callback.
            logger.debug(
                "tui renderer: invalidate callback raised",
                exc_info=True,
            )
