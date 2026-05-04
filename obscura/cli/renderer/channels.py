"""obscura.cli.renderer.channels — typed render-event taxonomy for v2.

The v1 renderer treated every renderable thing as an :class:`AgentEvent` and
printed inline. System events (rate-limit hits, supervisor heartbeats, daemon
outputs, "tip" messages) interleaved with the model's transcript.

v2 separates renderable events into four **channels** that occupy distinct
zones of the terminal:

- :class:`TranscriptEvent` → permanent scrollback (assistant text, thinking,
  tool calls, tool results)
- :class:`StatusEvent` → ephemeral live-region (spinner, current activity)
- :class:`Notification` → inline toast above the status line, auto-dismisses
  after ``ttl_seconds`` (rate-limit hits, supervisor heartbeats, daemon
  output, info hints)
- :class:`Banner` → sticky callout requiring user attention or persistent
  info (plan approval, arbiter kill, capability denial)

The :data:`RenderEvent` union is what :class:`ModernRenderer` consumes after
v2's adapter layer. Sources outside the agent loop (the InteractionBus,
the rate limiter, the kairos engine, etc.) emit ``Notification`` instances
directly — no need to disguise them as ``AgentEvent``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.core.types import AgentEvent

__all__ = [
    "Banner",
    "BannerKind",
    "Notification",
    "RenderEvent",
    "Severity",
    "StatusEvent",
    "TranscriptEvent",
    "from_agent_event",
    "from_agent_output",
]


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    SUCCESS = "success"


class BannerKind(StrEnum):
    PLAN_APPROVAL = "plan_approval"
    CAPABILITY_DENIAL = "capability_denial"
    ARBITER_KILL = "arbiter_kill"
    COMPACTION = "compaction"


# ---------------------------------------------------------------------------
# Channel dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptEvent:
    """Wraps an :class:`AgentEvent` destined for the permanent transcript.

    The renderer takes the underlying ``event`` and dispatches on its
    ``kind`` to draw text deltas, thinking panels, tool calls, results,
    etc. — same mapping as the legacy single-channel handler, just
    flagged so other channels can't accidentally write to scrollback.
    """

    event: "AgentEvent"


@dataclass(frozen=True)
class StatusEvent:
    """Ephemeral activity indicator for the live-region.

    Latest wins — pushing a new StatusEvent replaces the prior one. The
    renderer's spinner pulse advances per-frame regardless of how often
    StatusEvents arrive.

    To clear the status line (no activity), pass ``active=False``.
    """

    text: str = ""
    spinner: bool = True
    preview: str = ""
    active: bool = True


@dataclass(frozen=True)
class Notification:
    """Inline toast — one or more lines stacked above the status region.

    Auto-dismisses after :attr:`ttl_seconds`. ``source`` is a free-form
    label ("supervisor", "rate_limit", "kairos", "daemon:health-monitor")
    used both for the visible prefix and for de-duplicating rapid bursts
    of identical events. ``key``, when set, replaces any existing
    notification with the same key (useful for rolling progress updates
    so a single "task: 3/10" notification updates in place rather than
    spamming the stack).
    """

    title: str
    body: str = ""
    severity: Severity = Severity.INFO
    source: str = ""
    ttl_seconds: float = 5.0
    key: str = ""  # if set, replaces same-key entries instead of stacking
    created_at: float = field(default_factory=time.monotonic)

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl_seconds


@dataclass(frozen=True)
class Banner:
    """Sticky callout: persistent until explicitly dismissed.

    Used for plan approval, arbiter kill, capability denial — anything
    requiring user attention or that should remain visible across the
    next several render frames.
    """

    kind: BannerKind
    title: str
    body: str = ""
    actions: tuple[str, ...] = ()
    # Per-banner unique id used for replace-or-dismiss semantics.
    banner_id: str = ""


# Single union — what ModernRenderer.handle() accepts after the adapter layer.
RenderEvent = TranscriptEvent | StatusEvent | Notification | Banner


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


# AgentEventKind values that should land in the banner channel rather than
# the transcript. Other kinds default to TranscriptEvent.
_BANNER_AGENT_KINDS: frozenset[str] = frozenset(
    {
        "context_compact",
        "plan_approval_request",
    }
)


def from_agent_event(event: "AgentEvent") -> RenderEvent:
    """Route an :class:`AgentEvent` to the appropriate channel.

    Most events (TEXT_DELTA, TOOL_CALL, TOOL_RESULT, THINKING_DELTA,
    AGENT_DONE, TURN_*, ERROR) land in the transcript. Compaction and
    plan-approval requests go to the banner channel.
    """
    kind_value = (
        event.kind.value if hasattr(event.kind, "value") else str(event.kind)
    )
    if kind_value in _BANNER_AGENT_KINDS:
        # Re-route to a banner. The renderer's banner handler reads
        # title/body off the underlying event.
        if kind_value == "context_compact":
            return Banner(
                kind=BannerKind.COMPACTION,
                title="Context compacted",
                body=getattr(event, "text", "") or "",
            )
        return Banner(
            kind=BannerKind.PLAN_APPROVAL,
            title="Plan approval required",
            body=getattr(event, "text", "") or "",
            actions=("approve", "reject"),
        )
    return TranscriptEvent(event=event)


def from_agent_output(output: Any) -> Notification:
    """Convert an :class:`obscura.agent.interaction.AgentOutput` into a
    :class:`Notification`.

    Streamed outputs from supervised agents (LoopAgent / DaemonAgent in
    the fleet) used to be printed directly to stdout by the legacy
    ``render_agent_output``. Routing them through the notification
    channel means they stack above the prompt, auto-dismiss, and never
    interleave with the main loop's transcript again.

    The agent's name becomes the notification source. Final outputs
    (``is_final=True``) get a longer TTL so the "agent X said Y" line
    stays visible long enough to read before fading.
    """
    text = getattr(output, "text", "") or ""
    agent_name = getattr(output, "agent_name", "") or "agent"
    agent_id = getattr(output, "agent_id", "") or ""
    is_final = bool(getattr(output, "is_final", False))

    return Notification(
        title=agent_name if is_final else "",
        body=text,
        severity=Severity.INFO,
        source=f"agent:{agent_id or agent_name}",
        # Streaming chunks share a key keyed by agent_id so they update
        # in-place rather than stacking. Final outputs get a unique key
        # (timestamp-based) so they persist as a row in the stack.
        key=f"stream:{agent_id}" if not is_final else "",
        ttl_seconds=15.0 if is_final else 3.0,
    )
