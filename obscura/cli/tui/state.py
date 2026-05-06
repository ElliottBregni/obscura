"""obscura.cli.tui.state — Pydantic state for the full-screen TUI.

The TUI keeps **one mutable state container** (:class:`TUIState`) that
every layout component reads from and that the
:class:`obscura.cli.tui.renderer.TUIRenderer` mutates as agent events
arrive.

Design rules
------------
* All state is described by typed Pydantic models — no ad-hoc dicts.
* No lazy imports. Every dependency is at module top.
* The state is mutated **in place** (``model_config = {"frozen": False}``)
  because prompt-toolkit re-reads it on every frame; immutable rebuilds
  would churn allocation per keystroke.
* Each model carries enough information to render in isolation — a
  component does not have to know about siblings.
* Every entry has a stable ``id`` so the layout can diff/scroll/select
  by id rather than by index.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from obscura.cli.renderer.channels import Severity

__all__ = [
    "ApprovalRisk",
    "HUDState",
    "LiveRegionKind",
    "LiveRegionState",
    "NotificationItem",
    "RunningAgentSnapshot",
    "StyledRun",
    "ToolApprovalRequest",
    "TUIMode",
    "TUIState",
    "TranscriptEntry",
    "TranscriptKind",
    "make_tui_id",
]


def make_tui_id() -> str:
    """Generate a short opaque id for transcript entries / notifications."""
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TUIMode(StrEnum):
    """High-level TUI input mode. Drives the input box style + completer set."""

    CHAT = "chat"  # Default — message goes to the agent loop
    COMMAND = "command"  # Slash command being typed
    SEARCH = "search"  # Ctrl-R history search
    APPROVAL = "approval"  # Tool-approval modal in front
    PALETTE = "palette"  # Ctrl-K command palette in front
    AGENT_INSPECT = "agent_inspect"  # F2 agent inspector in front


class TranscriptKind(StrEnum):
    """Discriminator for :class:`TranscriptEntry`."""

    USER = "user"  # User-submitted prompt
    ASSISTANT = "assistant"  # Final assistant text
    THINKING = "thinking"  # Reasoning block
    TOOL_USE = "tool_use"  # Model invoked a tool
    TOOL_RESULT = "tool_result"  # Result of that tool call
    SYSTEM = "system"  # System notice (banner committed, /clear, etc.)
    ERROR = "error"  # Stream error or tool failure
    SLASH_OUTPUT = "slash_output"  # Captured Rich output from a /slash command
    NOTIFICATION_LOG = "notification_log"  # Notification archived to scrollback


class LiveRegionKind(StrEnum):
    """What the live region is currently showing."""

    IDLE = "idle"
    THINKING = "thinking"
    STREAMING = "streaming"
    TOOL_RUNNING = "tool_running"


class ApprovalRisk(StrEnum):
    """Risk classification for tool-approval modal."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ---------------------------------------------------------------------------
# Atoms
# ---------------------------------------------------------------------------


class StyledRun(BaseModel):
    """A single styled run of text.

    Directly serialisable to a prompt-toolkit ``(style, text)`` tuple via
    :meth:`as_pt`. The style string follows prompt-toolkit conventions:
    space-separated style tokens, optionally with class references
    (``"class:tool fg:#89b4fa bold"``).
    """

    model_config = ConfigDict(frozen=True)

    text: str
    style: str = ""

    def as_pt(self) -> tuple[str, str]:
        """Return the prompt-toolkit ``(style, text)`` tuple."""
        return (self.style, self.text)


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------


class TranscriptEntry(BaseModel):
    """One committed line group in the scrollback area.

    Each entry is rendered as a contiguous block. Tool-use and tool-result
    entries are linked via ``parent_id`` so the layout can collapse pairs.
    """

    model_config = ConfigDict(frozen=False)

    id: str = Field(default_factory=make_tui_id)
    kind: TranscriptKind
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    runs: list[StyledRun] = Field(default_factory=list)
    """The styled text content. Each run becomes a (style, text) tuple."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Free-form metadata (tool name, tool_use_id, exit code, etc.)."""

    parent_id: str | None = None
    """For TOOL_RESULT, the id of the matching TOOL_USE entry."""

    collapsed: bool = False
    """When True, the layout shows a one-line summary instead of full body."""


# ---------------------------------------------------------------------------
# Live region (ephemeral, redrawn per frame)
# ---------------------------------------------------------------------------


class LiveRegionState(BaseModel):
    """Bottom-of-screen activity indicator.

    Updated as TEXT_DELTA / THINKING_DELTA / TOOL_CALL events arrive;
    cleared on TURN_COMPLETE. The actual spinner animation advances on a
    timer in the Application — this state only carries semantic content.
    """

    model_config = ConfigDict(frozen=False)

    kind: LiveRegionKind = LiveRegionKind.IDLE
    label: str = ""
    """Short verb phrase: "thinking…", "running edit_file", "calling claude"."""

    preview: str = ""
    """Truncated tail of the latest delta (≤80 chars typically)."""

    full_text: str = ""
    """Complete streamed text for the active live region.

    The reveal-cursor tick (driven by ``app._reveal_tick``) advances
    ``reveal_pos`` along this buffer with jittered bursts so the
    visible ``preview`` types in smoothly rather than snapping. Empty
    when ``kind`` is IDLE / TOOL_RUNNING / TOOL_PENDING / ERROR.
    """

    reveal_pos: int = 0
    """How many chars of ``full_text`` are currently shown via ``preview``.

    Bumped in jittered ±30% bursts by the per-frame reveal tick so the
    streaming preview reads as organic typing instead of arriving in
    backend-shaped chunks. Reset to 0 by ``reset()`` and on transitions
    out of STREAMING / THINKING.
    """

    started_at_monotonic: float = Field(default_factory=time.monotonic)
    """Used to compute elapsed time without storing it."""

    spinner_idx: int = 0
    """Current spinner frame; the timer advances this."""

    @property
    def elapsed_s(self) -> float:
        """Seconds since live region became active (or 0 when idle)."""
        if self.kind == LiveRegionKind.IDLE:
            return 0.0
        return max(0.0, time.monotonic() - self.started_at_monotonic)

    def reset(self) -> None:
        """Return to IDLE."""
        self.kind = LiveRegionKind.IDLE
        self.label = ""
        self.preview = ""
        self.full_text = ""
        self.reveal_pos = 0
        self.spinner_idx = 0


# ---------------------------------------------------------------------------
# Notifications (auto-dismissing toasts)
# ---------------------------------------------------------------------------


class NotificationItem(BaseModel):
    """One stack entry. Mirrors :class:`renderer.channels.Notification`
    but is mutable so the TUI can age it out and apply replace-by-key.

    Created from a renderer-channel ``Notification`` via
    :meth:`from_channel`.
    """

    model_config = ConfigDict(frozen=False)

    id: str = Field(default_factory=make_tui_id)
    title: str = ""
    body: str = ""
    severity: Severity = Severity.INFO
    source: str = ""
    key: str = ""
    """When set, replacing-by-key is enabled — pushing a notification with
    the same key updates the existing entry instead of stacking a new one."""

    created_at_monotonic: float = Field(default_factory=time.monotonic)
    ttl_seconds: float = 5.0

    @property
    def expires_at_monotonic(self) -> float:
        return self.created_at_monotonic + self.ttl_seconds

    def is_expired(self, now: float | None = None) -> bool:
        """True when the notification has aged past its TTL."""
        ref = now if now is not None else time.monotonic()
        return ref >= self.expires_at_monotonic


# ---------------------------------------------------------------------------
# Banner (sticky, requires explicit dismiss)
# ---------------------------------------------------------------------------


class BannerState(BaseModel):
    """Sticky top-of-screen banner. ``None`` = no banner active."""

    model_config = ConfigDict(frozen=False)

    id: str = Field(default_factory=make_tui_id)
    kind: Literal["plan_approval", "capability_denial", "arbiter_kill", "compaction"]
    title: str
    body: str = ""
    actions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool approval modal
# ---------------------------------------------------------------------------


class ToolApprovalRequest(BaseModel):
    """Pending tool-approval question — drives the approval-modal float.

    Mirrors :class:`obscura.cli.widgets.ToolConfirmRequest` but adds a
    ``risk`` and ``preview`` field so the modal can give a richer
    "what will this do" summary. Today these are populated only when the
    caller pre-computes them — see ``docs/tui-deferred-rewrites.md``
    section "Pre-execution risk preview".
    """

    model_config = ConfigDict(frozen=True)

    tool_use_id: str
    tool_name: str
    tool_input: dict[str, Any]
    risk: ApprovalRisk = ApprovalRisk.LOW
    preview: str = ""
    """Optional "what will this do" string — diff, command preview, etc."""


# ---------------------------------------------------------------------------
# HUD (header + toolbar shared state)
# ---------------------------------------------------------------------------


class RunningAgentSnapshot(BaseModel):
    """One running supervised agent — rendered in the toolbar tree."""

    model_config = ConfigDict(frozen=False)

    name: str
    status: Literal["running", "waiting", "pending"] = "running"
    elapsed_s: float = 0.0
    iteration_count: int = 0
    last_tool: str = ""

    @property
    def elapsed_display(self) -> str:
        """Humanised elapsed string, "1m23s" / "47s"."""
        s = int(self.elapsed_s)
        if s < 60:
            return f"{s}s"
        m, sec = divmod(s, 60)
        return f"{m}m{sec:02d}s"


class HUDState(BaseModel):
    """Header + toolbar bag — read by header window and bottom toolbar.

    Updated by the runtime between turns; the layout components re-read
    on every frame so changes appear without an explicit redraw.
    """

    model_config = ConfigDict(frozen=False)

    # Identity
    backend: str
    model: str
    session_id: str
    session_title: str | None = None

    # Workspace
    branch: str | None = None
    workspace: str | None = None

    # Mode / context budget
    mode: TUIMode = TUIMode.CHAT
    permission_mode: str = "default"
    ctx_pct: int = 0
    ctx_tokens: int = 0
    ctx_window: int = 0

    # Streaming flag — drives prompt dimming.
    is_streaming: bool = False

    # Activity counters
    task_count: int = 0
    running_agents: list[RunningAgentSnapshot] = Field(default_factory=list)

    # Capability surface — tools registered with the session and MCP
    # servers connected for it. Populated at app startup and refreshed
    # by the agents-tick poll so newly-discovered MCP tools / hot-
    # registered tools appear without a restart.
    tool_count: int = 0

    # Per-server MCP status. Each entry is an ``MCPServerStatus``-shaped
    # dict (kept as ``dict`` here so this module doesn't pull in the
    # MCP protocol types just for typing): ``{"name": str, "state":
    # "connected"|"failed"|"unknown", "transport": str, "tool_count":
    # int, "error": str}``. The header reads ``state`` for the dot
    # colour; the Ctrl-K palette's "diagnose MCP servers" action reads
    # ``error`` for the per-server failure detail.
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]


# ---------------------------------------------------------------------------
# Top-level TUI state
# ---------------------------------------------------------------------------


class TUIState(BaseModel):
    """The single mutable state container for the TUI.

    Owned by :class:`obscura.cli.tui.app.ObscuraTUIApp`. Every layout
    component reads it; the renderer mutates it; overlays consult it
    when deciding whether to render.
    """

    model_config = ConfigDict(frozen=False, arbitrary_types_allowed=True)

    hud: HUDState

    # Permanent scrollback. Append-only; the layout truncates to the
    # configured retention size before rendering.
    transcript: list[TranscriptEntry] = Field(default_factory=list)

    # Ephemeral spinner / thinking-preview row.
    live: LiveRegionState = Field(default_factory=LiveRegionState)

    # Auto-dismissing toast stack. Bottom-most is newest.
    notifications: list[NotificationItem] = Field(default_factory=list)

    # Sticky callout (None = no banner).
    banner: BannerState | None = None

    # Modal float content. ``None`` = no modal.
    pending_approval: ToolApprovalRequest | None = None

    # Per-run flags consulted by layout components.
    show_agent_panel: bool = True
    show_thinking: bool = True

    # Transcript filter — ``"all"`` is the normal scrollback, while
    # ``"tools_only"`` (toggled with Ctrl+T) hides assistant prose and
    # shows only TOOL_USE / TOOL_RESULT entries. Useful for catching
    # up on long sessions where tool-call lines are buried under
    # narration.
    transcript_filter: Literal["all", "tools_only"] = "all"

    # Most recent ``maybe_truncate_result`` overflow file. The renderer
    # writes both fields when it detects the truncation marker; the
    # Ctrl-K palette's ``:open last large output`` action reads them
    # to launch ``$EDITOR``. Empty when no overflow has happened.
    last_overflow_path: str = ""
    last_overflow_tool: str = ""

    # ---- Mutators -------------------------------------------------------
    # Kept here (not on the renderer) so any caller — runtime, overlay,
    # slash-command — can update state without going through the renderer.

    def append_transcript(self, entry: TranscriptEntry) -> None:
        """Append a new entry; cap at 5000 entries to keep memory bounded."""
        self.transcript.append(entry)
        if len(self.transcript) > 5000:
            del self.transcript[: len(self.transcript) - 5000]

    def push_notification(self, item: NotificationItem) -> None:
        """Push a notification. Replaces any existing entry with the same key."""
        if item.key:
            self.notifications = [n for n in self.notifications if n.key != item.key]
        self.notifications.append(item)
        if len(self.notifications) > 12:
            del self.notifications[: len(self.notifications) - 12]

    def prune_notifications(self) -> int:
        """Drop expired notifications. Returns count removed."""
        now = time.monotonic()
        before = len(self.notifications)
        self.notifications = [n for n in self.notifications if not n.is_expired(now)]
        return before - len(self.notifications)

    def clear_banner(self) -> None:
        self.banner = None

    def open_approval(self, req: ToolApprovalRequest) -> None:
        self.pending_approval = req
        self.hud.mode = TUIMode.APPROVAL

    def close_approval(self) -> None:
        self.pending_approval = None
        self.hud.mode = TUIMode.CHAT
