r"""obscura.cli.renderer.ui_event — Stable TUI event contract.

The renderer consumes :class:`UiEvent` instances. Provider/tool/runtime
events flow through :class:`SignalNormalizer` (see :mod:`normalizer`),
which converts the codebase's intermediate :class:`AgentEvent`
representation into one or more ``UiEvent``\ s with mode-aware
``visibility`` so the renderer can stay provider-agnostic.

Existing :class:`AgentEvent` remains the SSE / event-store / replay
contract — ``UiEvent`` is purely the TUI-facing projection. Each
``UiEvent`` keeps a reference to the originating :class:`AgentEvent`
on ``raw`` so debug mode can surface provider details without the
renderer having to reach back into provider-specific shapes.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import StrEnum
from typing import Any

__all__ = [
    "DisplayMode",
    "UiEvent",
    "UiEventKind",
    "UiEventSource",
    "UiSeverity",
    "UiVisibility",
    "new_ui_event_id",
]


class UiEventKind(StrEnum):
    """High-level category the renderer dispatches on."""

    MESSAGE = "message"  # assistant / user / model text
    STATUS = "status"  # ephemeral activity indicator
    TOOL_CALL = "tool_call"  # pre-execution tool invocation
    TOOL_RESULT = "tool_result"  # post-execution tool outcome
    ERROR = "error"  # surfaced error (any source)
    DEBUG = "debug"  # debug-mode-only diagnostic
    TRACE = "trace"  # adapter/normalizer decision trace


class UiEventSource(StrEnum):
    """Logical origin of the event."""

    USER = "user"
    AGENT = "agent"  # final user-facing agent text
    MODEL = "model"  # raw model output (thinking, deltas)
    TOOL = "tool"  # tool call/result
    RUNTIME = "runtime"  # agent loop lifecycle (turn start, etc.)
    SYSTEM = "system"  # operator/CLI events (compaction, plan, etc.)
    PROVIDER = "provider"  # backend SDK system messages


class UiVisibility(StrEnum):
    """How the renderer should treat the event in normal mode."""

    NORMAL = "normal"  # always shown
    COLLAPSED = "collapsed"  # shown but truncated/folded
    HIDDEN = "hidden"  # never shown
    DEBUG_ONLY = "debug_only"  # shown only when display mode is DEBUG


class UiSeverity(StrEnum):
    """Severity classification for styling."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DisplayMode(StrEnum):
    """TUI display mode.

    NORMAL: clean transcript — user/assistant/tool only.
    DEBUG: includes raw provider payloads, adapter decisions, traces.
    """

    NORMAL = "normal"
    DEBUG = "debug"


def new_ui_event_id() -> str:
    """Compact, monotonically increasing-ish event id."""
    # uuid4 hex first 12 chars is enough for in-session uniqueness
    # and avoids importing a clock-based id generator.
    return uuid.uuid4().hex[:12]


def _empty_metadata() -> dict[str, Any]:
    return {}


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class UiEvent:
    """Stable TUI event contract.

    Mirrors the spec the renderer was designed against. Only ``kind``
    and ``source`` are required — everything else has a sensible
    default so adapters can construct partial events incrementally.
    """

    kind: UiEventKind
    source: UiEventSource
    id: str = field(default_factory=new_ui_event_id)
    ts: datetime = field(default_factory=_now_utc)
    title: str | None = None
    content: str | dict[str, Any] | list[Any] | None = None
    parent_id: str | None = None
    correlation_id: str | None = None
    provider: str | None = None
    model: str | None = None
    tool_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=_empty_metadata)
    raw: dict[str, Any] | str | None = None
    visibility: UiVisibility = UiVisibility.NORMAL
    severity: UiSeverity = UiSeverity.INFO
    # Wallclock monotonic ts in seconds — useful for renderer-side dedup
    # without touching the (timezone-aware) datetime.
    monotonic_ts: float = field(default_factory=time.monotonic)

    def is_visible(self, mode: DisplayMode) -> bool:
        """Should this event render in ``mode``?

        ``HIDDEN`` is never shown. ``DEBUG_ONLY`` shows only when mode
        is DEBUG. Everything else (NORMAL, COLLAPSED) always renders;
        ``COLLAPSED`` is a hint for the renderer to fold the content,
        not to drop it.
        """
        if self.visibility == UiVisibility.HIDDEN:
            return False
        if self.visibility == UiVisibility.DEBUG_ONLY:
            return mode == DisplayMode.DEBUG
        return True
