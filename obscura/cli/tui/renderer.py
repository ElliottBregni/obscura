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

import time
from collections.abc import Callable

from obscura.cli.renderer.channels import (
    Banner,
    BannerKind,
    Notification,
    StatusEvent,
    TranscriptEvent,
    from_agent_event,
)
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


# ---------------------------------------------------------------------------
# Banner-kind ↔ TUI banner-kind table
# ---------------------------------------------------------------------------

_BANNER_KIND_MAP: dict[BannerKind, str] = {
    BannerKind.PLAN_APPROVAL: "plan_approval",
    BannerKind.CAPABILITY_DENIAL: "capability_denial",
    BannerKind.ARBITER_KILL: "arbiter_kill",
    BannerKind.COMPACTION: "compaction",
}


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

    # ------------------------------------------------------------------
    # RendererProtocol surface
    # ------------------------------------------------------------------

    def handle(self, event: AgentEvent) -> None:
        """Route a single :class:`AgentEvent` to the appropriate channel.

        See :func:`obscura.cli.renderer.channels.from_agent_event` for the
        routing rules. After dispatch, expired notifications are pruned
        and the optional ``invalidate`` callback is fired so the owning
        ``Application`` can redraw.
        """
        rendered = from_agent_event(event)
        if isinstance(rendered, TranscriptEvent):
            self._handle_transcript(rendered.event)
        elif isinstance(rendered, StatusEvent):
            self._handle_status(rendered)
        elif isinstance(rendered, Notification):
            self._handle_notification(rendered)
        elif isinstance(rendered, Banner):
            self._handle_banner(rendered)

        # Cheap, idempotent — drop expired toasts on every tick.
        self._state.prune_notifications()
        self._fire_invalidate()

    def finish(self) -> None:
        """Flush any pending buffers and reset the live region.

        Called at the end of a turn (and again on cancellation). Safe to
        call repeatedly — flushing an empty buffer is a no-op.
        """
        self._flush_thinking()
        self._flush_text()
        self._state.live.reset()
        self._state.hud.is_streaming = False
        self._fire_invalidate()

    def get_accumulated_text(self) -> str:
        """Return all accumulated assistant text for this turn.

        Mirrors :meth:`StreamRenderer.get_accumulated_text`: includes
        already-flushed text plus any unflushed buffer contents and any
        unflushed thinking buffer contents (so the agent loop can capture
        partial output even if the turn was cancelled).
        """
        parts: list[str] = []
        parts.extend(self._all_text)
        parts.extend(self._text_buf)
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
            self._thinking_buf.append(event.text)
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
        """Flush ``_text_buf`` as a single ASSISTANT transcript entry."""
        if not self._text_buf:
            return
        text = "".join(self._text_buf)
        self._text_buf.clear()
        if not text.strip():
            return
        entry = TranscriptEntry(
            kind=TranscriptKind.ASSISTANT,
            runs=[StyledRun(text=text, style=_STYLE_ASSISTANT)],
        )
        self._state.append_transcript(entry)

    def _flush_thinking(self) -> None:
        """Flush ``_thinking_buf`` as a single THINKING transcript entry."""
        if not self._thinking_buf:
            self._in_thinking = False
            return
        text = "".join(self._thinking_buf)
        self._thinking_buf.clear()
        self._in_thinking = False
        if not text.strip():
            return
        self._thinking_blocks.append(text)
        entry = TranscriptEntry(
            kind=TranscriptKind.THINKING,
            runs=[StyledRun(text=text, style=_STYLE_THINKING)],
        )
        self._state.append_transcript(entry)

    # ------------------------------------------------------------------
    # Tool / error emission
    # ------------------------------------------------------------------

    def _emit_tool_call(self, event: AgentEvent) -> None:
        """Emit a TOOL_USE transcript entry from a TOOL_CALL event."""
        name = event.tool_name or "tool"
        # Thin formatting — the dedicated formatter module will replace
        # this with richer styling when wired in.
        summary = self._summarize_tool_call(name, event.tool_input)
        runs: list[StyledRun] = [
            StyledRun(text=name, style=f"{_STYLE_TOOL_CALL} bold"),
        ]
        if summary:
            runs.append(StyledRun(text=" "))
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
        live.preview = summary
        live.started_at_monotonic = time.monotonic()

    def _emit_tool_result(self, event: AgentEvent) -> None:
        """Emit a TOOL_RESULT transcript entry from a TOOL_RESULT event."""
        raw = (event.tool_result or "").rstrip()
        snippet = raw[:300]
        style = _STYLE_TOOL_RESULT_ERR if event.is_error else _STYLE_TOOL_RESULT
        runs: list[StyledRun] = []
        if snippet:
            runs.append(StyledRun(text=snippet, style=style))
        else:
            runs.append(StyledRun(text="(empty result)", style=style))
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
        """Refresh ``state.live`` to show a tail of the thinking buffer."""
        live = self._state.live
        if live.kind == LiveRegionKind.IDLE:
            live.started_at_monotonic = time.monotonic()
        live.kind = LiveRegionKind.THINKING
        live.label = "thinking"
        live.preview = self._tail("".join(self._thinking_buf))

    def _update_live_streaming(self) -> None:
        """Refresh ``state.live`` to show a tail of the text buffer."""
        live = self._state.live
        if live.kind == LiveRegionKind.IDLE:
            live.started_at_monotonic = time.monotonic()
        live.kind = LiveRegionKind.STREAMING
        live.label = "streaming"
        live.preview = self._tail("".join(self._text_buf))

    @staticmethod
    def _tail(text: str, *, limit: int = 80) -> str:
        """Trim ``text`` to a single-line preview suitable for the live row."""
        flat = text.replace("\n", " ").strip()
        if len(flat) <= limit:
            return flat
        return "..." + flat[-(limit - 3):]

    # ------------------------------------------------------------------
    # Tool-call summary (thin placeholder — formatter will replace)
    # ------------------------------------------------------------------

    @staticmethod
    def _summarize_tool_call(name: str, args: dict[str, object]) -> str:
        """Produce a one-line summary of ``args`` for the TOOL_USE entry.

        Intentionally minimal — the formatter module will replace this
        with the richer per-tool summarisation once wired in.
        """
        if not args:
            return ""
        # Common path-bearing keys — surface the most informative one.
        for key in ("path", "file_path", "command", "url", "query"):
            value = args.get(key)
            if isinstance(value, str) and value:
                return f"{key}={value}"
        # Fall back to a short comma-joined list of keys.
        keys = ", ".join(sorted(str(k) for k in args)[:4])
        return f"({keys})" if keys else ""

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
            # event loop.
            pass
