"""Tests for the v2 system-event rendering pipeline.

Covers the four layers that turn SDK system messages (TaskStartedMessage,
TaskProgressMessage, TaskNotificationMessage, MirrorErrorMessage,
RateLimitEvent) into terminal toasts:

1. ``ClaudeIteratorAdapter`` — SDK message → typed StreamChunk.
2. ``AgentLoopV2`` — StreamChunk → AgentEvent.
3. ``from_agent_event`` — AgentEvent → channel (TranscriptEvent /
   Notification / Banner).
4. ``ModernRenderer._notify_*`` + ``_render_notifications`` — AgentEvent
   → in-memory Notification → styled inline toast.

The final ``TestEndToEndPipeline`` class drives a stub backend through
``AgentLoopV2`` into a real ``ModernRenderer`` to confirm the full chain
fires for every kind without interleaving with assistant text.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from obscura.cli.renderer.channels import (
    Banner,
    Notification,
    Severity,
    TranscriptEvent,
    from_agent_event,
)
from obscura.cli.renderer.modern.renderer import ModernRenderer
from obscura.core.agent_loop_v2 import AgentLoopV2, AgentLoopV2Config
from obscura.core.enums.agent import AgentEventKind, ChunkKind
from obscura.core.stream import ClaudeIteratorAdapter
from obscura.core.tools import ToolRegistry
from obscura.core.types import AgentEvent, BackendCapabilities, Message, StreamChunk


# ---------------------------------------------------------------------------
# Synthetic SDK messages
# ---------------------------------------------------------------------------
#
# We don't import the real claude_agent_sdk classes here because the adapter
# matches by ``type(item).__name__`` to stay loose-coupled. Local dataclasses
# with the right name + attribute shape are sufficient for routing tests
# and keep the test independent of the SDK's import side-effects.


@dataclass
class TaskStartedMessage:
    task_id: str
    description: str
    uuid: str = "u1"
    session_id: str = "s1"
    tool_use_id: str | None = None
    task_type: str | None = None


@dataclass
class TaskProgressMessage:
    task_id: str
    description: str
    usage: dict[str, Any]
    uuid: str = "u1"
    session_id: str = "s1"
    tool_use_id: str | None = None
    last_tool_name: str | None = None


@dataclass
class TaskNotificationMessage:
    task_id: str
    status: str
    output_file: str
    summary: str
    uuid: str = "u1"
    session_id: str = "s1"
    tool_use_id: str | None = None


@dataclass
class MirrorErrorMessage:
    error: str
    key: Any = None


@dataclass
class _RateLimitInfo:
    status: str
    rate_limit_type: str | None = None
    utilization: float | None = None
    resets_at: int | None = None


@dataclass
class RateLimitEvent:
    rate_limit_info: _RateLimitInfo
    uuid: str = "u1"
    session_id: str = "s1"


# ---------------------------------------------------------------------------
# Layer 1 — ClaudeIteratorAdapter
# ---------------------------------------------------------------------------


class _EmptyAsyncIter:
    """Minimal async iterator placeholder; the adapter only calls ``_adapt``
    in these tests so the source is never iterated."""

    def __aiter__(self):  # pragma: no cover — never called
        return self

    async def __anext__(self):  # pragma: no cover
        raise StopAsyncIteration


@pytest.fixture
def adapter() -> ClaudeIteratorAdapter:
    return ClaudeIteratorAdapter(_EmptyAsyncIter())


class TestClaudeAdapterRoutesSdkSubclasses:
    def test_task_started_becomes_chunk(self, adapter: ClaudeIteratorAdapter) -> None:
        msg = TaskStartedMessage(task_id="tk_42", description="Search code")
        chunks = adapter._adapt(msg)
        assert len(chunks) == 1
        ch = chunks[0]
        assert ch.kind is ChunkKind.TASK_STARTED
        assert ch.text == "Search code"
        assert ch.tool_use_id == "tk_42"
        assert ch.raw is msg

    def test_task_progress_carries_last_tool_name(
        self, adapter: ClaudeIteratorAdapter
    ) -> None:
        msg = TaskProgressMessage(
            task_id="tk_42",
            description="Working",
            usage={"tokens": 1234},
            last_tool_name="Read",
        )
        chunks = adapter._adapt(msg)
        assert chunks[0].kind is ChunkKind.TASK_PROGRESS
        assert chunks[0].tool_name == "Read"
        assert chunks[0].tool_use_id == "tk_42"

    def test_task_notification_uses_summary_as_text(
        self, adapter: ClaudeIteratorAdapter
    ) -> None:
        msg = TaskNotificationMessage(
            task_id="tk_42",
            status="completed",
            output_file="/tmp/out",
            summary="Found 12 matches",
        )
        chunks = adapter._adapt(msg)
        assert chunks[0].kind is ChunkKind.TASK_NOTIFICATION
        assert chunks[0].text == "Found 12 matches"
        assert chunks[0].tool_use_id == "tk_42"

    def test_mirror_error_routes_to_chunk(self, adapter: ClaudeIteratorAdapter) -> None:
        msg = MirrorErrorMessage(error="connection timeout")
        chunks = adapter._adapt(msg)
        assert chunks[0].kind is ChunkKind.MIRROR_ERROR
        assert chunks[0].text == "connection timeout"

    def test_rate_limit_event_carries_status_text(
        self, adapter: ClaudeIteratorAdapter
    ) -> None:
        info = _RateLimitInfo(
            status="allowed_warning", utilization=0.87, rate_limit_type="five_hour"
        )
        msg = RateLimitEvent(rate_limit_info=info)
        chunks = adapter._adapt(msg)
        assert chunks[0].kind is ChunkKind.RATE_LIMIT
        assert chunks[0].text == "allowed_warning"
        assert chunks[0].raw is msg


# ---------------------------------------------------------------------------
# Layer 2 — channels.from_agent_event
# ---------------------------------------------------------------------------


class TestFromAgentEventRouting:
    def test_text_delta_remains_in_transcript(self) -> None:
        ev = AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="hi")
        routed = from_agent_event(ev)
        assert isinstance(routed, TranscriptEvent)

    def test_context_compact_routes_to_banner(self) -> None:
        ev = AgentEvent(kind=AgentEventKind.CONTEXT_COMPACT, text="trimmed 12k tokens")
        routed = from_agent_event(ev)
        assert isinstance(routed, Banner)
        assert routed.title == "Context compacted"

    def test_task_started_routes_to_notification(self) -> None:
        ev = AgentEvent(
            kind=AgentEventKind.TASK_STARTED,
            text="Search the codebase",
            tool_use_id="tk_1",
        )
        routed = from_agent_event(ev)
        assert isinstance(routed, Notification)
        assert routed.title == "Task started"
        assert routed.body == "Search the codebase"
        assert routed.severity is Severity.INFO
        assert routed.key == "task:tk_1"

    def test_task_progress_includes_last_tool_in_body(self) -> None:
        ev = AgentEvent(
            kind=AgentEventKind.TASK_PROGRESS,
            text="Working",
            tool_name="Read",
            tool_use_id="tk_1",
        )
        routed = from_agent_event(ev)
        assert isinstance(routed, Notification)
        assert "Working" in routed.body
        assert "using Read" in routed.body
        # Same key as TASK_STARTED → progress replaces started in place.
        assert routed.key == "task:tk_1"

    @pytest.mark.parametrize(
        ("status", "expected_severity"),
        [
            ("completed", Severity.SUCCESS),
            ("failed", Severity.ERROR),
            ("stopped", Severity.WARN),
            ("", Severity.INFO),
        ],
    )
    def test_task_notification_severity_by_status(
        self, status: str, expected_severity: Severity
    ) -> None:
        raw = TaskNotificationMessage(
            task_id="tk_1", status=status, output_file="", summary="done"
        )
        ev = AgentEvent(
            kind=AgentEventKind.TASK_NOTIFICATION,
            text="done",
            tool_use_id="tk_1",
            raw=raw,
        )
        routed = from_agent_event(ev)
        assert isinstance(routed, Notification)
        assert routed.severity is expected_severity

    @pytest.mark.parametrize(
        ("status", "expected_severity", "expected_title"),
        [
            ("rejected", Severity.ERROR, "Rate limit hit"),
            ("allowed_warning", Severity.WARN, "Rate limit warning"),
            ("allowed", Severity.INFO, "Rate limit"),
        ],
    )
    def test_rate_limit_severity_by_status(
        self,
        status: str,
        expected_severity: Severity,
        expected_title: str,
    ) -> None:
        info = _RateLimitInfo(status=status, utilization=0.5)
        raw = RateLimitEvent(rate_limit_info=info)
        ev = AgentEvent(kind=AgentEventKind.RATE_LIMIT_WARNING, text=status, raw=raw)
        routed = from_agent_event(ev)
        assert isinstance(routed, Notification)
        assert routed.severity is expected_severity
        assert routed.title == expected_title

    def test_rate_limit_rejected_has_long_ttl(self) -> None:
        info = _RateLimitInfo(status="rejected")
        raw = RateLimitEvent(rate_limit_info=info)
        ev = AgentEvent(
            kind=AgentEventKind.RATE_LIMIT_WARNING, text="rejected", raw=raw
        )
        routed = from_agent_event(ev)
        assert isinstance(routed, Notification)
        assert routed.ttl_seconds >= 30.0

    def test_mirror_error_routes_to_warn_notification(self) -> None:
        ev = AgentEvent(kind=AgentEventKind.MIRROR_ERROR, text="upstream 5xx")
        routed = from_agent_event(ev)
        assert isinstance(routed, Notification)
        assert routed.severity is Severity.WARN
        assert routed.body == "upstream 5xx"
        assert routed.key == "mirror_error"


# ---------------------------------------------------------------------------
# Layer 3 — ModernRenderer.handle + _notify_* + _render_notifications
# ---------------------------------------------------------------------------


@pytest.fixture
def renderer() -> ModernRenderer:
    return ModernRenderer()


class TestRendererHandlePushesNotifications:
    """``handle()`` should push the matching notification onto the stack
    instead of committing to scrollback."""

    def test_task_started_pushes_notification(self, renderer: ModernRenderer) -> None:
        ev = AgentEvent(
            kind=AgentEventKind.TASK_STARTED, text="Indexing", tool_use_id="tk_a"
        )
        renderer.handle(ev)
        assert len(renderer._notifications) == 1
        n = renderer._notifications[0]
        assert n.title == "Task started"
        assert n.body == "Indexing"
        assert n.key == "task:tk_a"

    def test_task_progress_replaces_started_in_place(
        self, renderer: ModernRenderer
    ) -> None:
        renderer.handle(
            AgentEvent(
                kind=AgentEventKind.TASK_STARTED,
                text="Indexing",
                tool_use_id="tk_a",
            )
        )
        renderer.handle(
            AgentEvent(
                kind=AgentEventKind.TASK_PROGRESS,
                text="Indexing",
                tool_name="Read",
                tool_use_id="tk_a",
            )
        )
        # Same key → in-place replace, stack length unchanged.
        assert len(renderer._notifications) == 1
        n = renderer._notifications[0]
        assert n.title == "Task progress"
        assert "using Read" in n.body

    def test_rate_limit_resets_at_renders_human_time(
        self, renderer: ModernRenderer
    ) -> None:
        import time

        info = _RateLimitInfo(
            status="allowed_warning",
            utilization=0.87,
            rate_limit_type="five_hour",
            resets_at=int(time.time()) + 42 * 60,
        )
        ev = AgentEvent(
            kind=AgentEventKind.RATE_LIMIT_WARNING,
            text="allowed_warning",
            raw=RateLimitEvent(rate_limit_info=info),
        )
        renderer.handle(ev)
        n = renderer._notifications[0]
        # Body should contain percent + window + relative reset.
        assert "87%" in n.body
        assert "five-hour" in n.body
        assert "resets in" in n.body and "m" in n.body

    def test_mirror_error_pushes_warn_notification(
        self, renderer: ModernRenderer
    ) -> None:
        ev = AgentEvent(kind=AgentEventKind.MIRROR_ERROR, text="db offline")
        renderer.handle(ev)
        n = renderer._notifications[0]
        assert n.severity is Severity.WARN
        assert n.body == "db offline"

    def test_unrelated_event_does_not_push_notification(
        self, renderer: ModernRenderer
    ) -> None:
        renderer.handle(AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="hi"))
        assert renderer._notifications == []


class TestRenderNotificationsFormat:
    """``_render_notifications`` should produce the v2 left-bar style
    instead of the legacy ASCII format."""

    def test_left_bar_present_in_rendered_line(self, renderer: ModernRenderer) -> None:
        renderer.add_notification(
            Notification(
                title="Task started",
                body="indexing",
                severity=Severity.INFO,
                source="task",
            )
        )
        lines = renderer._render_notifications()
        assert len(lines) == 1
        # Left bar character appears in the rendered output.
        assert "▎" in lines[0]

    @pytest.mark.parametrize(
        ("severity", "icon"),
        [
            (Severity.INFO, "ℹ"),  # noqa: RUF001 — Unicode info icon
            (Severity.WARN, "⚠"),
            (Severity.ERROR, "✗"),
            (Severity.SUCCESS, "✓"),
        ],
    )
    def test_icon_per_severity(
        self, renderer: ModernRenderer, severity: Severity, icon: str
    ) -> None:
        renderer.add_notification(
            Notification(title="x", body="y", severity=severity, source="s")
        )
        lines = renderer._render_notifications()
        assert icon in lines[0]

    def test_long_body_truncated_with_ellipsis(self, renderer: ModernRenderer) -> None:
        renderer._width = 60
        renderer.add_notification(
            Notification(
                title="t",
                body="x" * 200,
                severity=Severity.INFO,
                source="s",
            )
        )
        lines = renderer._render_notifications()
        # The visible substring (post-ANSI) should not exceed terminal width.
        # Conservative: just assert ellipsis was added.
        assert "..." in lines[0]

    def test_empty_stack_renders_nothing(self, renderer: ModernRenderer) -> None:
        assert renderer._render_notifications() == []


# ---------------------------------------------------------------------------
# End-to-end pipeline — backend → AgentLoopV2 → ModernRenderer
# ---------------------------------------------------------------------------


class _SystemEventBackend:
    """Stub backend that emits a fixed sequence of system-event chunks
    interleaved with text.  Models what ``ClaudeIteratorAdapter`` would
    produce after seeing real SDK messages.
    """

    def __init__(self, chunks: list[StreamChunk]) -> None:
        self._chunks = chunks

    @property
    def name(self) -> str:
        return "system-events"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=True,
            supports_tools=True,
            supports_thinking=False,
            supports_native_tools=False,
        )

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def stream(
        self,
        messages: list[Message] | None = None,
        **_kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        for ch in self._chunks:
            yield ch


class TestEndToEndPipeline:
    """Drive AgentLoopV2 with a stub backend and a real ModernRenderer to
    prove every system-event kind ends up as a Notification (not in the
    transcript) and assistant text continues to render normally."""

    @pytest.mark.asyncio
    async def test_all_system_events_become_notifications(self) -> None:
        # Build a fake ``raw`` for the rate-limit chunk so the renderer can
        # fish out status/utilization just like real SDK objects.
        info = _RateLimitInfo(
            status="allowed_warning",
            utilization=0.92,
            rate_limit_type="five_hour",
        )
        rate_limit_raw = RateLimitEvent(rate_limit_info=info)

        backend = _SystemEventBackend(
            [
                StreamChunk(kind=ChunkKind.TEXT_DELTA, text="Working on it. "),
                StreamChunk(
                    kind=ChunkKind.TASK_STARTED,
                    text="Indexing repo",
                    tool_use_id="tk_42",
                ),
                StreamChunk(
                    kind=ChunkKind.TASK_PROGRESS,
                    text="Indexing repo",
                    tool_name="Grep",
                    tool_use_id="tk_42",
                ),
                StreamChunk(
                    kind=ChunkKind.RATE_LIMIT,
                    text="allowed_warning",
                    raw=rate_limit_raw,
                ),
                StreamChunk(
                    kind=ChunkKind.TASK_NOTIFICATION,
                    text="Indexed 1280 files",
                    tool_use_id="tk_42",
                    raw=TaskNotificationMessage(
                        task_id="tk_42",
                        status="completed",
                        output_file="",
                        summary="Indexed 1280 files",
                    ),
                ),
                StreamChunk(
                    kind=ChunkKind.MIRROR_ERROR,
                    text="upstream 502",
                ),
                StreamChunk(kind=ChunkKind.TEXT_DELTA, text="Done."),
            ]
        )
        loop = AgentLoopV2(
            backend=backend,
            registry=ToolRegistry(),
            config=AgentLoopV2Config(max_turns=1),
        )
        renderer = ModernRenderer()
        events: list[AgentEvent] = []
        try:
            async for ev in loop.run("hello"):
                events.append(ev)
                renderer.handle(ev)
        finally:
            renderer.finish()

        kinds = [e.kind for e in events]
        # All five system kinds make it through the loop as AgentEvents.
        assert AgentEventKind.TASK_STARTED in kinds
        assert AgentEventKind.TASK_PROGRESS in kinds
        assert AgentEventKind.TASK_NOTIFICATION in kinds
        assert AgentEventKind.RATE_LIMIT_WARNING in kinds
        assert AgentEventKind.MIRROR_ERROR in kinds

        # Assistant text deltas survive — system events did not eat them.
        text_events = [e for e in events if e.kind is AgentEventKind.TEXT_DELTA]
        assert "".join(e.text for e in text_events) == "Working on it. Done."

        # Renderer state: TASK_STARTED/PROGRESS/NOTIFICATION share key
        # ``task:tk_42`` so only the latest survives. Rate-limit and
        # mirror-error get their own slots. So we expect 3 notifications.
        assert len(renderer._notifications) == 3
        titles = {n.title for n in renderer._notifications}
        assert "Task completed" in titles
        assert "Rate limit warning" in titles
        assert "Session mirror error" in titles

        # The accumulated text on the renderer is exactly the assistant
        # text — system messages never leaked into the transcript stream.
        assert renderer.get_accumulated_text() == "Working on it. Done."
