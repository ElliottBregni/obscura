"""obscura.cli.renderer.adapters.runtime — Default AgentEvent adapter.

Catches every :class:`AgentEvent` that doesn't match a more specific
adapter and projects it onto a :class:`UiEvent` with the right
``kind`` / ``source`` / ``visibility``.

Visibility rules (NORMAL mode):
    * MESSAGE assistant text       → NORMAL
    * MODEL thinking deltas        → DEBUG_ONLY (noisy by default)
    * TOOL_CALL / TOOL_RESULT      → NORMAL
    * RUNTIME lifecycle (turn,     → DEBUG_ONLY (renderer needs them
        agent_done, etc.)            for buffering, see normalizer
                                     pass-through path)
    * SYSTEM events (compaction,   → NORMAL (banner-bound)
        plan approval)
    * PROVIDER system messages     → NORMAL (notification-bound)
    * ERROR                        → NORMAL
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import override

from obscura.cli.renderer.adapters.base import EventAdapter
from obscura.cli.renderer.ui_event import (
    UiEvent,
    UiEventKind,
    UiEventSource,
    UiSeverity,
    UiVisibility,
)
from obscura.core.enums.agent import AgentEventKind
from obscura.core.types import AgentEvent

# AgentEventKinds that the renderer's frame buffer needs to see in
# normal mode for layout reasons (turn boundaries, terminal events)
# even though they're conceptually runtime/lifecycle. They pass
# through with visibility=NORMAL and the renderer's existing handlers
# pick them up; the *display* of these is the empty side-effect of
# flushing buffers, not a transcript line.
_LAYOUT_RUNTIME_KINDS: frozenset[AgentEventKind] = frozenset(
    {
        AgentEventKind.TURN_START,
        AgentEventKind.TURN_COMPLETE,
        AgentEventKind.AGENT_DONE,
    }
)

_DEBUG_ONLY_RUNTIME_KINDS: frozenset[AgentEventKind] = frozenset(
    {
        AgentEventKind.AGENT_START,
        AgentEventKind.AGENT_STOP,
        AgentEventKind.STOP_CHECK,
        AgentEventKind.PREFLIGHT_PASS,
        AgentEventKind.PREFLIGHT_FAIL,
        AgentEventKind.SUBAGENT_START,
        AgentEventKind.SESSION_PAUSED,
        AgentEventKind.USER_INPUT,
        AgentEventKind.CORRECTION_INJECTED,
    }
)

# AgentEventKinds that render as banners (system-routed).
_BANNER_KINDS: frozenset[AgentEventKind] = frozenset(
    {
        AgentEventKind.CONTEXT_COMPACT,
        AgentEventKind.PLAN_APPROVAL_REQUEST,
    }
)

# AgentEventKinds that render as notifications (provider system messages).
_PROVIDER_NOTIFICATION_KINDS: frozenset[AgentEventKind] = frozenset(
    {
        AgentEventKind.TASK_STARTED,
        AgentEventKind.TASK_PROGRESS,
        AgentEventKind.TASK_NOTIFICATION,
        AgentEventKind.RATE_LIMIT_WARNING,
        AgentEventKind.MIRROR_ERROR,
    }
)


class RuntimeEventAdapter(EventAdapter):
    """Default adapter — handles every :class:`AgentEvent` shape.

    Always last in the adapter chain; ``handles()`` always returns
    True. More specific adapters (MCP, shell) run first and can
    override field shaping.
    """

    @override
    def handles(self, event: AgentEvent) -> bool:  # noqa: ARG002
        return True

    @override
    def adapt(self, event: AgentEvent) -> Iterable[UiEvent]:
        try:
            kind = event.kind
        except AttributeError:
            yield UiEvent(
                kind=UiEventKind.ERROR,
                source=UiEventSource.RUNTIME,
                title="Malformed AgentEvent",
                content=repr(event),
                severity=UiSeverity.ERROR,
            )
            return

        if kind == AgentEventKind.TEXT_DELTA:
            yield UiEvent(
                kind=UiEventKind.MESSAGE,
                source=UiEventSource.AGENT,
                content=event.text,
                metadata={"turn": event.turn, "delta": True},
                raw=_safe_raw(event),
                visibility=UiVisibility.NORMAL,
            )
            return

        if kind == AgentEventKind.THINKING_DELTA:
            # Thinking is interesting in debug mode, distracting in
            # normal mode. The renderer's status bar still gets a
            # short preview via the normalizer's status-line side
            # channel; the transcript event itself is debug-only.
            yield UiEvent(
                kind=UiEventKind.MESSAGE,
                source=UiEventSource.MODEL,
                title="thinking",
                content=event.text,
                metadata={"turn": event.turn, "delta": True, "thinking": True},
                raw=_safe_raw(event),
                visibility=UiVisibility.NORMAL,  # actual visibility decided
                                                  # by renderer's thinking pane
            )
            return

        if kind == AgentEventKind.TOOL_CALL:
            yield UiEvent(
                kind=UiEventKind.TOOL_CALL,
                source=UiEventSource.TOOL,
                tool_name=event.tool_name or None,
                title=event.tool_name or "tool",
                content=dict(event.tool_input or {}),
                correlation_id=event.tool_use_id or None,
                metadata={"turn": event.turn},
                raw=_safe_raw(event),
                visibility=UiVisibility.NORMAL,
            )
            return

        if kind == AgentEventKind.TOOL_RESULT:
            severity = UiSeverity.ERROR if event.is_error else UiSeverity.INFO
            yield UiEvent(
                kind=UiEventKind.TOOL_RESULT,
                source=UiEventSource.TOOL,
                tool_name=event.tool_name or None,
                title=event.tool_name or "tool",
                content=event.tool_result or "",
                correlation_id=event.tool_use_id or None,
                metadata={"turn": event.turn, "is_error": bool(event.is_error)},
                raw=_safe_raw(event),
                visibility=UiVisibility.NORMAL,
                severity=severity,
            )
            return

        if kind == AgentEventKind.TOOL_CALL_FAILURE:
            yield UiEvent(
                kind=UiEventKind.ERROR,
                source=UiEventSource.TOOL,
                tool_name=event.tool_name or None,
                title=f"{event.tool_name or 'tool'} failed",
                content=event.tool_result or event.text or "tool execution failed",
                correlation_id=event.tool_use_id or None,
                metadata={"turn": event.turn, "is_error": True},
                raw=_safe_raw(event),
                visibility=UiVisibility.NORMAL,
                severity=UiSeverity.ERROR,
            )
            return

        if kind == AgentEventKind.ERROR:
            yield UiEvent(
                kind=UiEventKind.ERROR,
                source=UiEventSource.RUNTIME,
                content=event.text or "",
                metadata={"turn": event.turn},
                raw=_safe_raw(event),
                visibility=UiVisibility.NORMAL,
                severity=UiSeverity.ERROR,
            )
            return

        if kind in _BANNER_KINDS:
            yield UiEvent(
                kind=UiEventKind.STATUS,
                source=UiEventSource.SYSTEM,
                title=str(kind.value),
                content=event.text or "",
                metadata={"turn": event.turn, "channel": "banner", "agent_kind": kind.value},
                raw=_safe_raw(event),
                visibility=UiVisibility.NORMAL,
            )
            return

        if kind in _PROVIDER_NOTIFICATION_KINDS:
            yield UiEvent(
                kind=UiEventKind.STATUS,
                source=UiEventSource.PROVIDER,
                title=str(kind.value),
                content=event.text or "",
                metadata={
                    "turn": event.turn,
                    "channel": "notification",
                    "agent_kind": kind.value,
                },
                raw=_safe_raw(event),
                visibility=UiVisibility.NORMAL,
            )
            return

        if kind in _LAYOUT_RUNTIME_KINDS:
            # Pass-through for renderer's frame-buffer bookkeeping —
            # not a transcript line itself, but the renderer needs the
            # signal. Visibility=NORMAL because the renderer's handler
            # is what produces the actual visible side effect (flush,
            # spinner, etc.).
            yield UiEvent(
                kind=UiEventKind.TRACE,
                source=UiEventSource.RUNTIME,
                title=str(kind.value),
                metadata={"turn": event.turn, "agent_kind": kind.value, "layout": True},
                raw=_safe_raw(event),
                visibility=UiVisibility.NORMAL,
            )
            return

        if kind in _DEBUG_ONLY_RUNTIME_KINDS:
            yield UiEvent(
                kind=UiEventKind.DEBUG,
                source=UiEventSource.RUNTIME,
                title=str(kind.value),
                content=event.text or "",
                metadata={"turn": event.turn, "agent_kind": kind.value},
                raw=_safe_raw(event),
                visibility=UiVisibility.DEBUG_ONLY,
            )
            return

        # Unknown / unhandled — emit as debug-only so debug mode can
        # still see it, but normal mode stays clean.
        yield UiEvent(
            kind=UiEventKind.DEBUG,
            source=UiEventSource.RUNTIME,
            title=str(getattr(kind, "value", kind)),
            content=event.text or "",
            metadata={"turn": event.turn, "agent_kind": str(kind)},
            raw=_safe_raw(event),
            visibility=UiVisibility.DEBUG_ONLY,
        )


def _safe_raw(event: AgentEvent) -> dict[str, object]:
    """Snapshot AgentEvent fields for ``UiEvent.raw``.

    Avoids holding references to backend SDK objects via
    ``event.raw`` — the renderer reads ``raw`` for debug display
    only, so a flat dict of the displayable fields is enough.
    """
    return {
        "kind": str(getattr(event.kind, "value", event.kind)),
        "text": event.text,
        "tool_name": event.tool_name,
        "tool_input": dict(event.tool_input or {}),
        "tool_result": event.tool_result,
        "tool_use_id": event.tool_use_id,
        "is_error": event.is_error,
        "turn": event.turn,
    }
