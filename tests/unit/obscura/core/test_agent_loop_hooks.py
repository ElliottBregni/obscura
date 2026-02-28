"""Tests for AgentLoop integration with HookRegistry and EventStore."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator, Callable, override

import pytest

from obscura.core.agent_loop import AgentLoop
from obscura.core.event_store import SQLiteEventStore, SessionStatus
from obscura.core.hooks import HookRegistry
from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    AgentEvent,
    AgentEventKind,
    Backend,
    BackendCapabilities,
    ChunkKind,
    HookPoint,
    Message,
    NativeHandle,
    Role,
    SessionRef,
    StreamChunk,
    ToolSpec,
)


# ---------------------------------------------------------------------------
# Helpers (same pattern as existing test_agent_loop.py)
# ---------------------------------------------------------------------------


def _make_text_chunks(text: str) -> list[StreamChunk]:
    chunks: list[StreamChunk] = []
    for word in text.split(" "):
        chunks.append(StreamChunk(kind=ChunkKind.TEXT_DELTA, text=word + " "))
    chunks.append(StreamChunk(kind=ChunkKind.DONE))
    return chunks


def _make_tool_call_chunks(
    tool_name: str,
    tool_input: dict[str, Any],
) -> list[StreamChunk]:
    chunks: list[StreamChunk] = []
    chunks.append(StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name=tool_name))
    chunks.append(
        StreamChunk(
            kind=ChunkKind.TOOL_USE_DELTA,
            tool_input_delta=json.dumps(tool_input),
        )
    )
    chunks.append(StreamChunk(kind=ChunkKind.DONE))
    return chunks


class MockBackend:
    def __init__(self, turn_responses: list[list[StreamChunk]]) -> None:
        self._turns = list(turn_responses)
        self._call_count = 0
        self._registry = ToolRegistry()

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        if self._call_count < len(self._turns):
            chunks = self._turns[self._call_count]
        else:
            chunks = [StreamChunk(kind=ChunkKind.DONE)]
        self._call_count += 1
        for chunk in chunks:
            yield chunk

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        return Message(role=Role.ASSISTANT, content=[], raw=None)

    async def create_session(self, **kwargs: Any) -> SessionRef:
        return SessionRef(session_id="s", backend=Backend.COPILOT)

    async def resume_session(self, ref: SessionRef) -> None:
        return None

    async def list_sessions(self) -> list[SessionRef]:
        return []

    async def delete_session(self, ref: SessionRef) -> None:
        return None

    def register_tool(self, spec: ToolSpec) -> None:
        self._registry.register(spec)

    def register_hook(self, hook: HookPoint, callback: Callable[..., Any]) -> None:
        return None

    def get_tool_registry(self) -> ToolRegistry:
        return self._registry

    @property
    def native(self) -> NativeHandle:
        return NativeHandle()

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()


def _make_registry(*specs: ToolSpec) -> ToolRegistry:
    reg = ToolRegistry()
    for s in specs:
        reg.register(s)
    return reg


# ---------------------------------------------------------------------------
# Tests: Hooks wired into loop
# ---------------------------------------------------------------------------


class TestAgentLoopHooks:
    @pytest.mark.asyncio
    async def test_before_hook_fires_for_every_event(self) -> None:
        hooks = HookRegistry()
        seen: list[AgentEventKind] = []

        @hooks.before()
        def track(event: AgentEvent) -> AgentEvent:
            seen.append(event.kind)
            return event

        backend = MockBackend([_make_text_chunks("hello")])
        loop = AgentLoop(backend, _make_registry(), hooks=hooks)

        events = [e async for e in loop.run("hi")]
        # The hook should have seen every event the loop emitted
        assert len(seen) == len(events)
        assert seen == [e.kind for e in events]

    @pytest.mark.asyncio
    async def test_after_hook_fires_for_every_event(self) -> None:
        hooks = HookRegistry()
        after_seen: list[AgentEventKind] = []

        @hooks.after()
        def track(event: AgentEvent) -> None:
            after_seen.append(event.kind)

        backend = MockBackend([_make_text_chunks("hello")])
        loop = AgentLoop(backend, _make_registry(), hooks=hooks)

        events = [e async for e in loop.run("hi")]
        # After-hooks fire for every yielded event
        assert len(after_seen) == len(events)

    @pytest.mark.asyncio
    async def test_before_hook_can_suppress_text_delta(self) -> None:
        hooks = HookRegistry()

        @hooks.before(AgentEventKind.TEXT_DELTA)
        def suppress(_event: AgentEvent) -> None:
            return None

        backend = MockBackend([_make_text_chunks("secret")])
        loop = AgentLoop(backend, _make_registry(), hooks=hooks)

        events = [e async for e in loop.run("hi")]
        text_events = [e for e in events if e.kind == AgentEventKind.TEXT_DELTA]
        assert len(text_events) == 0

    @pytest.mark.asyncio
    async def test_before_hook_can_modify_event(self) -> None:
        hooks = HookRegistry()

        @hooks.before(AgentEventKind.TEXT_DELTA)
        def redact(event: AgentEvent) -> AgentEvent:
            return AgentEvent(
                kind=event.kind,
                text="[redacted] ",
                turn=event.turn,
            )

        backend = MockBackend([_make_text_chunks("secret data")])
        loop = AgentLoop(backend, _make_registry(), hooks=hooks)

        events = [e async for e in loop.run("hi")]
        text_events = [e for e in events if e.kind == AgentEventKind.TEXT_DELTA]
        assert all(e.text == "[redacted] " for e in text_events)

    @pytest.mark.asyncio
    async def test_hooks_fire_around_tool_calls(self) -> None:
        hooks = HookRegistry()
        tool_events: list[tuple[str, AgentEventKind]] = []

        @hooks.before(AgentEventKind.TOOL_CALL)
        def before_tool(event: AgentEvent) -> AgentEvent:
            tool_events.append(("before", event.kind))
            return event

        @hooks.after(AgentEventKind.TOOL_RESULT)
        def after_result(event: AgentEvent) -> None:
            tool_events.append(("after_result", event.kind))

        spec = ToolSpec(
            name="ping", description="Ping", parameters={}, handler=lambda: "pong"
        )
        turn1 = _make_tool_call_chunks("ping", {})
        turn2 = _make_text_chunks("done")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec), hooks=hooks)

        [e async for e in loop.run("go")]

        assert ("before", AgentEventKind.TOOL_CALL) in tool_events
        assert ("after_result", AgentEventKind.TOOL_RESULT) in tool_events


# ---------------------------------------------------------------------------
# Tests: EventStore wired into loop
# ---------------------------------------------------------------------------


class TestAgentLoopEventStore:
    @pytest.mark.asyncio
    async def test_events_persisted_to_store(self, tmp_path: Path) -> None:
        store = SQLiteEventStore(tmp_path / "test.db")
        backend = MockBackend([_make_text_chunks("hello")])
        loop = AgentLoop(backend, _make_registry(), event_store=store)

        events = [e async for e in loop.run("hi", session_id="sess-1")]
        stored = await store.get_events("sess-1")

        assert len(stored) == len(events)
        assert [e.kind.value for e in stored] == [
            e.kind.value for e in events
        ]

    @pytest.mark.asyncio
    async def test_session_created_automatically(self, tmp_path: Path) -> None:
        store = SQLiteEventStore(tmp_path / "test.db")
        backend = MockBackend([_make_text_chunks("hi")])
        loop = AgentLoop(backend, _make_registry(), event_store=store)

        [e async for e in loop.run("go", session_id="auto-sess")]

        session = await store.get_session("auto-sess")
        assert session is not None
        assert session.status == SessionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_session_marked_completed_on_success(
        self, tmp_path: Path
    ) -> None:
        store = SQLiteEventStore(tmp_path / "test.db")
        backend = MockBackend([_make_text_chunks("done")])
        loop = AgentLoop(backend, _make_registry(), event_store=store)

        [e async for e in loop.run("go", session_id="s")]

        session = await store.get_session("s")
        assert session is not None
        assert session.status == SessionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_session_marked_failed_on_error(
        self, tmp_path: Path
    ) -> None:
        class FailBackend(MockBackend):
            async def stream(
                self, prompt: str, **kw: Any
            ) -> AsyncIterator[StreamChunk]:
                raise ConnectionError("boom")
                yield  # type: ignore[misc]  # noqa: E501

        store = SQLiteEventStore(tmp_path / "test.db")
        backend = FailBackend([])
        loop = AgentLoop(backend, _make_registry(), event_store=store)

        [e async for e in loop.run("go", session_id="s")]

        session = await store.get_session("s")
        assert session is not None
        assert session.status == SessionStatus.FAILED

    @pytest.mark.asyncio
    async def test_no_persistence_without_session_id(
        self, tmp_path: Path
    ) -> None:
        store = SQLiteEventStore(tmp_path / "test.db")
        backend = MockBackend([_make_text_chunks("hi")])
        loop = AgentLoop(backend, _make_registry(), event_store=store)

        # No session_id → no persistence
        [e async for e in loop.run("go")]

        sessions = await store.list_sessions()
        assert len(sessions) == 0

    @pytest.mark.asyncio
    async def test_tool_results_persisted(self, tmp_path: Path) -> None:
        store = SQLiteEventStore(tmp_path / "test.db")
        spec = ToolSpec(
            name="echo",
            description="Echo",
            parameters={"type": "object", "properties": {"msg": {"type": "string"}}},
            handler=lambda msg: f"echo:{msg}",
        )
        turn1 = _make_tool_call_chunks("echo", {"msg": "test"})
        turn2 = _make_text_chunks("done")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec), event_store=store)

        [e async for e in loop.run("go", session_id="s")]

        stored = await store.get_events("s")
        tool_results = [e for e in stored if e.kind == AgentEventKind.TOOL_RESULT]
        assert len(tool_results) == 1
        assert "echo:test" in tool_results[0].payload.get("tool_result", "")


# ---------------------------------------------------------------------------
# Tests: Hooks + EventStore together
# ---------------------------------------------------------------------------


class TestAgentLoopHooksAndStore:
    @pytest.mark.asyncio
    async def test_hooks_and_store_work_together(self, tmp_path: Path) -> None:
        hooks = HookRegistry()
        store = SQLiteEventStore(tmp_path / "test.db")

        hook_count = 0

        @hooks.after()
        def count(_event: AgentEvent) -> None:
            nonlocal hook_count
            hook_count += 1

        backend = MockBackend([_make_text_chunks("hi")])
        loop = AgentLoop(
            backend, _make_registry(), hooks=hooks, event_store=store
        )

        events = [e async for e in loop.run("go", session_id="s")]
        stored = await store.get_events("s")

        # All three should agree on count
        assert hook_count == len(events)
        assert len(stored) == len(events)

    @pytest.mark.asyncio
    async def test_modified_event_is_what_gets_stored(
        self, tmp_path: Path
    ) -> None:
        hooks = HookRegistry()
        store = SQLiteEventStore(tmp_path / "test.db")

        @hooks.before(AgentEventKind.TEXT_DELTA)
        def tag(event: AgentEvent) -> AgentEvent:
            return AgentEvent(
                kind=event.kind,
                text=f"[tagged]{event.text}",
                turn=event.turn,
            )

        backend = MockBackend([_make_text_chunks("raw")])
        loop = AgentLoop(
            backend, _make_registry(), hooks=hooks, event_store=store
        )

        [e async for e in loop.run("go", session_id="s")]
        stored = await store.get_events("s")
        text_events = [e for e in stored if e.kind == AgentEventKind.TEXT_DELTA]
        assert all("[tagged]" in e.payload.get("text", "") for e in text_events)
