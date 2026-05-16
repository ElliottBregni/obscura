"""obscura.cli.tui.formatter — pure event-to-state primitive translator.

This module sits one layer below the renderer-channels router
(:mod:`obscura.cli.renderer.channels`). Given a routed
:class:`~obscura.cli.renderer.channels.RenderEvent` (transcript / status /
notification / banner) — or, for transcript events specifically, the raw
:class:`~obscura.core.types.AgentEvent` — produce the corresponding
mutable Pydantic state primitive defined in :mod:`obscura.cli.tui.state`.

Design rules
------------
* **Pure functions.** No side effects, no shared state, no caches. Each
  call returns a freshly constructed Pydantic model.
* **No lazy imports.** Every dependency is imported at module top.
* **Re-use, don't reinvent.** Visual choices come from
  :mod:`obscura.cli.renderer.modern.theme` (Catppuccin Mocha), and the
  legacy :class:`~obscura.cli.render.StreamRenderer` is the reference
  for which colors map to which event kind. The formatter mirrors that
  mapping in styled-run form rather than printing ANSI.
* **Never duplicate routing.** Callers route via
  :func:`obscura.cli.renderer.channels.from_agent_event`; the formatter
  trusts the channel decision and only translates content.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from obscura.cli.renderer.channels import Banner as ChannelBanner
from obscura.cli.renderer.channels import Notification as ChannelNotification
from obscura.cli.renderer.channels import StatusEvent
from obscura.cli.renderer.modern.theme import (
    ERROR_HEX,
    GREEN,
    MAUVE,
    MUTED_HEX,
    OK_HEX,
    SAPPHIRE,
    TEAL,
    THINKING_HEX,
    TOOL_HEX,
)
from obscura.cli.tool_summaries import classify_tool
from obscura.cli.tui.state import (
    BannerState,
    LiveRegionKind,
    LiveRegionState,
    NotificationItem,
    StyledRun,
    TranscriptEntry,
    TranscriptKind,
)
from obscura.core.enums.agent import AgentEventKind
from obscura.core.types import AgentEvent

logger = logging.getLogger(__name__)

__all__ = [
    "format_banner",
    "format_notification",
    "format_slash_output",
    "format_status_event",
    "format_transcript_event",
    "format_user_prompt",
]


# ---------------------------------------------------------------------------
# Internal style constants — keep all colour decisions here so theme drift
# in tools elsewhere doesn't desynchronize the TUI.
# ---------------------------------------------------------------------------

# Re-keyed under prompt-toolkit conventions: ``"fg:#xxxxxx [bold] [italic]"``.
_STYLE_USER = f"fg:{OK_HEX} bold"
_STYLE_ASSISTANT = ""  # default fg
_STYLE_THINKING = f"fg:{THINKING_HEX} italic"
_STYLE_THINKING_BAR = f"fg:{THINKING_HEX}"
_STYLE_TOOL_NAME = f"fg:{TOOL_HEX} bold"
_STYLE_TOOL_GLYPH = f"fg:{TOOL_HEX}"
_STYLE_TOOL_DETAIL = f"fg:{MUTED_HEX}"

# Per-kind glyph + name colour. Mirrors ``_TOOL_KIND_STYLES`` in the
# bordered modern renderer so a Copilot session looks the same in
# both surfaces. The "tag" is a short bracketed label (``MCP``,
# ``PLUG``, ``$``, ``TASK``) prefixed before the tool name; empty
# means no decoration (default native tools).
_TOOL_KIND_STYLES: dict[str, tuple[str, str]] = {
    "native": (TOOL_HEX, ""),
    "shell": (GREEN.hex, "$"),
    "mcp": (SAPPHIRE.hex, "MCP"),
    "plugin": (TEAL.hex, "PLUG"),
    "delegation": (MAUVE.hex, "TASK"),
}
_STYLE_TOOL_RESULT = f"fg:{MUTED_HEX}"
_STYLE_ERROR = f"fg:{ERROR_HEX} bold"
_STYLE_SYSTEM = f"fg:{MUTED_HEX} italic"
_STYLE_SLASH = f"fg:{SAPPHIRE.hex}"

# Glyph alphabet from the legacy renderer; kept in sync with
# ``obscura/cli/render.py`` so the visuals match across renderers.
_GLYPH_TOOL = "⏺"
_GLYPH_BAR = "▎"
_GLYPH_ERR = "✗"

# How much tool output (raw text) to keep on screen for an *error*.
_ERROR_RESULT_CAP = 2000
# Single-line preview length for successful tool results.
_RESULT_PREVIEW_LEN = 80
# ANSI CSI/OSC strippers, used when slash-command captures contain colour
# escapes from Rich. We intentionally keep this minimal — full-fidelity
# ANSI->styled-run conversion is out of scope for the first cut.
_ANSI_CSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)")
_C0_CONTROL_RE = re.compile(r"[\x00-\x08\x0B-\x1F\x7F]+")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _runs_for_text(text: str, style: str = "") -> list[StyledRun]:
    """Wrap a flat string as a single styled run.

    Empty strings produce an empty list so transcript entries never carry
    zero-width runs that the layout would have to skip.
    """
    if not text:
        return []
    return [StyledRun(text=text, style=style)]


def _runs_for_thinking(text: str) -> list[StyledRun]:
    """Render reasoning text Claude-Code-style: a coloured left bar plus
    dim italic body. Keeps a single run per line so the layout can wrap
    cleanly without disturbing the bar prefix."""
    body = text.strip()
    if not body:
        return []
    runs: list[StyledRun] = []
    for idx, line in enumerate(body.split("\n")):
        if idx > 0:
            runs.append(StyledRun(text="\n", style=""))
        runs.append(StyledRun(text=f"  {_GLYPH_BAR} ", style=_STYLE_THINKING_BAR))
        runs.append(StyledRun(text=line, style=_STYLE_THINKING))
    return runs


def _format_tool_input(name: str, input_dict: dict[str, Any]) -> str:
    """Build a human-readable single-line "detail" for a tool call.

    Special cases:

    * ``path`` argument → bare path string (file the tool will touch).
    * ``bash`` / ``run_shell`` → the command string.
    * ``edit_text_file`` / ``write_text_file`` → ``path (N lines)``.
    * Anything else → compact JSON, capped at 120 chars.
    """
    if not input_dict:
        return ""

    if name in {"bash", "run_shell"}:
        cmd = str(input_dict.get("command", "")).strip()
        if cmd:
            return cmd

    if name in {"edit_text_file", "write_text_file"}:
        path = str(input_dict.get("path", "")).strip()
        content = input_dict.get("content") or input_dict.get("new_text") or ""
        line_count = str(content).count("\n") + (1 if content else 0)
        if path and line_count:
            return f"{path} ({line_count} lines)"
        if path:
            return path

    if "path" in input_dict:
        path = str(input_dict.get("path", "")).strip()
        if path:
            return path

    try:
        compact = json.dumps(input_dict, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        # Tool inputs are usually JSON-clean but pydantic models /
        # custom objects can slip through. Falling back to ``repr``
        # keeps the renderer alive; deep logs surface the cause.
        logger.debug(
            "tui formatter: json.dumps failed for tool input, falling back to repr",
            exc_info=True,
        )
        compact = repr(input_dict)
    if len(compact) > 120:
        compact = compact[:117] + "..."
    return compact


def _runs_for_tool_call(name: str, input_dict: dict[str, Any]) -> list[StyledRun]:
    """Lay out a tool-call line: glyph + (kind tag) + bold tool name + dim detail."""
    detail = _format_tool_input(name, input_dict)
    kind = classify_tool(name)
    color_hex, tag = _TOOL_KIND_STYLES.get(kind, _TOOL_KIND_STYLES["native"])
    runs: list[StyledRun] = [
        StyledRun(
            text=f"  {_GLYPH_TOOL} ",
            style=f"fg:{color_hex} bold",
        ),
    ]
    if tag:
        runs.append(
            StyledRun(text=f"{tag} ", style=f"fg:{color_hex}"),
        )
    runs.append(
        StyledRun(text=name, style=f"fg:{color_hex} bold"),
    )
    if detail:
        runs.append(StyledRun(text=" ", style=""))
        runs.append(StyledRun(text=detail, style=_STYLE_TOOL_DETAIL))
    return runs


def _truncate_preview(text: str, max_len: int = _RESULT_PREVIEW_LEN) -> str:
    """Single-line, fixed-width preview suitable for the tool-result row."""
    if not text:
        return ""
    flat = text.replace("\n", " ").strip()
    if len(flat) <= max_len:
        return flat
    return flat[:max_len] + " ..."


def _runs_for_tool_result(
    name: str,
    result_text: str,
    is_error: bool,
) -> list[StyledRun]:
    """Render a tool result row.

    Errors get the full text capped at ``_ERROR_RESULT_CAP`` characters
    plus an ``✗`` glyph; successes get a single-line preview limited to
    ``_RESULT_PREVIEW_LEN`` characters with " ..." appended on overflow.
    The ``name`` argument is currently unused but kept as part of the
    helper's signature so per-tool result formatters can be plugged in
    later (e.g. by delegating to ``modern.tool_renderers``).
    """
    del name  # reserved for future per-tool adapters
    if not result_text:
        return []
    if is_error:
        capped = result_text[:_ERROR_RESULT_CAP]
        return [
            StyledRun(text=f"    {_GLYPH_ERR} ", style=_STYLE_ERROR),
            StyledRun(text=capped, style=_STYLE_ERROR),
        ]
    snippet = _truncate_preview(result_text)
    if not snippet:
        return []
    return [StyledRun(text=f"    {snippet}", style=_STYLE_TOOL_RESULT)]


def _strip_ansi(text: str) -> str:
    """Best-effort ANSI removal used as the slash-output fallback."""
    if not text:
        return ""
    cleaned = _ANSI_CSI_RE.sub("", text)
    cleaned = _ANSI_OSC_RE.sub("", cleaned)
    cleaned = _C0_CONTROL_RE.sub("", cleaned)
    return cleaned


def _ansi_to_runs(text: str, fallback_style: str = "") -> list[StyledRun]:
    """Parse Rich/ANSI console output into styled runs for the transcript."""
    if not text:
        return []
    text = text.replace("\r", "")
    try:
        fragments = to_formatted_text(ANSI(text))
        runs: list[StyledRun] = []
        for frag in fragments:
            style = str(frag[0]) if len(frag) > 0 else fallback_style
            run_text = str(frag[1]) if len(frag) > 1 else ""
            if not run_text:
                continue
            runs.append(StyledRun(text=run_text, style=style or fallback_style))
        return runs
    except Exception:
        logger.debug("ansi parse failed; falling back to stripped text", exc_info=True)
        cleaned = _strip_ansi(text)
        return [StyledRun(text=cleaned, style=fallback_style)] if cleaned else []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def format_transcript_event(event: AgentEvent) -> TranscriptEntry:
    """Convert one :class:`AgentEvent` destined for the scrollback into a
    fully populated :class:`TranscriptEntry`.

    Handles the event kinds that the transcript channel cares about:

    * :class:`~obscura.core.enums.agent.AgentEventKind.TEXT_DELTA` →
      assistant text.
    * :class:`~obscura.core.enums.agent.AgentEventKind.THINKING_DELTA` →
      reasoning block.
    * :class:`~obscura.core.enums.agent.AgentEventKind.TOOL_CALL` → tool
      use line; ``metadata`` carries ``tool_name``, ``tool_use_id``, and
      the original ``tool_input`` dict.
    * :class:`~obscura.core.enums.agent.AgentEventKind.TOOL_RESULT` →
      tool result row; ``parent_id`` is set to ``tool_use_id`` so the
      layout can collapse paired entries.
    * :class:`~obscura.core.enums.agent.AgentEventKind.ERROR` → error
      row.
    * :class:`~obscura.core.enums.agent.AgentEventKind.USER_INPUT` →
      treated as a USER entry when it slips through the stream.
    * :class:`~obscura.core.enums.agent.AgentEventKind.TURN_START` /
      ``TURN_COMPLETE`` / ``AGENT_DONE`` produce empty SYSTEM entries —
      the caller decides whether to drop or display them.

    Anything else falls back to a SYSTEM entry containing a single dim
    summary run, so unknown event kinds remain visible during dev rather
    than being silently swallowed.
    """
    kind = event.kind

    if kind == AgentEventKind.TEXT_DELTA:
        return TranscriptEntry(
            kind=TranscriptKind.ASSISTANT,
            runs=_runs_for_text(event.text, _STYLE_ASSISTANT),
        )

    if kind == AgentEventKind.THINKING_DELTA:
        return TranscriptEntry(
            kind=TranscriptKind.THINKING,
            runs=_runs_for_thinking(event.text),
        )

    if kind == AgentEventKind.TOOL_CALL:
        return TranscriptEntry(
            kind=TranscriptKind.TOOL_USE,
            runs=_runs_for_tool_call(event.tool_name, dict(event.tool_input)),
            metadata={
                "tool_name": event.tool_name,
                "tool_use_id": event.tool_use_id,
                "tool_input": dict(event.tool_input),
            },
        )

    if kind == AgentEventKind.TOOL_RESULT:
        return TranscriptEntry(
            kind=TranscriptKind.TOOL_RESULT,
            runs=_runs_for_tool_result(
                event.tool_name,
                event.tool_result or "",
                bool(event.is_error),
            ),
            metadata={
                "tool_name": event.tool_name,
                "tool_use_id": event.tool_use_id,
                "is_error": bool(event.is_error),
            },
            parent_id=event.tool_use_id or None,
        )

    if kind == AgentEventKind.ERROR:
        return TranscriptEntry(
            kind=TranscriptKind.ERROR,
            runs=[StyledRun(text=event.text or "(error)", style=_STYLE_ERROR)],
            metadata={"error_text": event.text or ""},
        )

    if kind == AgentEventKind.USER_INPUT:
        return format_user_prompt(event.text)

    if kind in (
        AgentEventKind.TURN_START,
        AgentEventKind.TURN_COMPLETE,
        AgentEventKind.AGENT_DONE,
    ):
        return TranscriptEntry(
            kind=TranscriptKind.SYSTEM,
            runs=[],
            metadata={"event_kind": kind.value},
        )

    # Fallback — keep diagnostic visibility for any kind we did not enumerate.
    summary = f"[{kind.value}] {event.text}" if event.text else f"[{kind.value}]"
    return TranscriptEntry(
        kind=TranscriptKind.SYSTEM,
        runs=[StyledRun(text=summary, style=_STYLE_SYSTEM)],
        metadata={"event_kind": kind.value},
    )


def format_status_event(status_event: StatusEvent) -> LiveRegionState:
    """Translate a :class:`StatusEvent` into a fresh
    :class:`LiveRegionState`.

    The returned state reflects the *intended* live-region content; the
    caller mutates the long-lived ``TUIState.live`` in place by copying
    fields off the result. We always return an idle state when
    ``status_event.active`` is False so the caller can simply assign the
    fields and forget about per-flag logic.
    """
    if not status_event.active:
        return LiveRegionState(kind=LiveRegionKind.IDLE)

    label = status_event.text or ""
    preview = status_event.preview or ""

    # Heuristic: the legacy renderer uses "thinking…" / "running …" /
    # "calling …" labels. Map those onto the matching live-region kind so
    # the spinner glyph and colour line up with the activity.
    lower = label.lower()
    if "running" in lower:
        kind = LiveRegionKind.TOOL_RUNNING
    elif "calling" in lower or "stream" in lower:
        kind = LiveRegionKind.STREAMING
    elif label:
        kind = LiveRegionKind.THINKING
    else:
        kind = LiveRegionKind.IDLE

    return LiveRegionState(kind=kind, label=label, preview=preview)


def format_notification(channel_notification: ChannelNotification) -> NotificationItem:
    """Build a mutable :class:`NotificationItem` from the immutable
    renderer-channels :class:`Notification` dataclass.

    The two types share the same fields; the difference is mutability +
    Pydantic validation. ``ttl_seconds`` and ``key`` are preserved so the
    TUI's replace-by-key and TTL-pruning behaviour still works.
    """
    return NotificationItem(
        title=channel_notification.title,
        body=channel_notification.body,
        severity=channel_notification.severity,
        source=channel_notification.source,
        key=channel_notification.key,
        ttl_seconds=channel_notification.ttl_seconds,
    )


def format_banner(channel_banner: ChannelBanner) -> BannerState:
    """Translate a renderer-channels :class:`Banner` into the TUI's
    :class:`BannerState`.

    The TUI's banner kind is a literal-string union so we route through
    ``.value`` defensively; channels.py uses ``BannerKind`` (StrEnum)
    whose value strings already match the literal alphabet.
    """
    kind_str = (
        channel_banner.kind.value
        if hasattr(channel_banner.kind, "value")
        else str(channel_banner.kind)
    )
    return BannerState(
        kind=kind_str,  # type: ignore[arg-type]
        title=channel_banner.title,
        body=channel_banner.body,
        actions=list(channel_banner.actions),
    )


def format_user_prompt(prompt_text: str) -> TranscriptEntry:
    """Build a :class:`TranscriptKind.USER` transcript entry from the
    user's submitted prompt. Empty input still produces a (empty-runs)
    entry so the caller can always anchor a turn boundary in scrollback.
    """
    return TranscriptEntry(
        kind=TranscriptKind.USER,
        runs=_runs_for_text(prompt_text, _STYLE_USER),
        metadata={"raw_text": prompt_text},
    )


def format_slash_output(rich_capture: str) -> TranscriptEntry:
    """Build a :class:`TranscriptKind.SLASH_OUTPUT` entry from captured
    Rich console output.

    ``rich_capture`` may contain ANSI escape codes from Rich. Parse them
    back into styled runs so slash-command output stays visually native
    inside the full-screen TUI instead of collapsing to plain text.
    """
    runs = _ansi_to_runs(rich_capture or "", _STYLE_SLASH)
    if not runs:
        cleaned = _strip_ansi(rich_capture or "")
        runs = _runs_for_text(cleaned, _STYLE_SLASH)
    return TranscriptEntry(
        kind=TranscriptKind.SLASH_OUTPUT,
        runs=runs,
        metadata={
            "raw_text": rich_capture or "",
            "plain_text": _strip_ansi(rich_capture or ""),
        },
    )
