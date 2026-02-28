"""Tests for interruptible sessions: pause, resume, mid-run user input."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator, override

import pytest

from obscura.core.agent_loop import AgentLoop
from obscura.core.event_store import SQLiteEventStore, SessionStatus
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
# Helpers
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
    chunks.append(StreamChunk(kind=ChunkKind.TOOL_USE_END))
    chunks.append(StreamChunk(kind=ChunkKind.DONE))
    return chunks


class MockBackend:
    """Deterministic backend that yields pre-configured turn responses."""

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

    def register_hook(self, hook: HookPoint, callback: Any) -> None:
        return None

    def get_tool_registry(self) -> ToolRegistry:
        return self._registry

    @property
    def native(self) -> NativeHandle:
        return NativeHandle()

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()


def _echo_handler(msg: str = "") -> str:
    return f"echo:{msg}"


def _make_registry(*specs: ToolSpec) -> ToolRegistry:
    reg = ToolRegistry()
    for s in specs:
        reg.register(s)
    return reg


# ---------------------------------------------------------------------------
# Pause tests
# ---------------------------------------------------------------------------


class TestPause:
    @pytest.mark.asyncio
    async def test_pause_at_turn_boundary(self, tmp_path: Path) -> None:
        """Pause after first turn emits SESSION_PAUSED and sets session PAUSED."""
        store = SQLiteEventStore(tmp_path / "test.db")
        # Two turns of text (but pause should stop after first)
        backend = MockBackend([
            _make_text_chunks("hello"),
            _make_text_chunks("world"),
        ])
        loop = AgentLoop(backend, _make_registry(), event_store=store, max_turns=5)

        events: list[AgentEvent] = []
        async for event in loop.run("go", session_id="s1"):
            events.append(event)
            # Request pause after first TURN_COMPLETE
            if event.kind == AgentEventKind.TURN_COMPLETE:
                loop.request_pause()

        # Should have: TURN_START, TEXT_DELTA(s), TURN_COMPLETE, AGENT_DONE
        # Wait — model produced text only (no tools), so it ends with AGENT_DONE
        # The pause check happens AFTER TURN_COMPLETE but BEFORE checking for
        # no tool calls. Since text-only turn has no tools, AGENT_DONE is emitted
        # before pause is checked. Let me adjust: pause should be checked before
        # the tool check decision. Let me verify actual behavior.
        kinds = [e.kind for e in events]
        # Pause check fires at turn boundary BEFORE "no tools → done",
        # so even text-only responses pause when requested.
        assert AgentEventKind.SESSION_PAUSED in kinds
        assert AgentEventKind.AGENT_DONE not in kinds

    @pytest.mark.asyncio
    async def test_pause_during_tool_loop(self, tmp_path: Path) -> None:
        """Pause during a multi-turn tool loop stops at turn boundary."""
        store = SQLiteEventStore(tmp_path / "test.db")
        spec = ToolSpec(
            name="ping", description="Ping", parameters={}, handler=lambda: "pong"
        )
        # Turn 1: tool call → Turn 2: text
        backend = MockBackend([
            _make_tool_call_chunks("ping", {}),
            _make_text_chunks("done"),
        ])
        loop = AgentLoop(
            backend, _make_registry(spec), event_store=store, max_turns=5
        )

        events: list[AgentEvent] = []
        async for event in loop.run("go", session_id="s1"):
            events.append(event)
            # Request pause after the first TURN_COMPLETE (after tool call turn)
            if event.kind == AgentEventKind.TURN_COMPLETE and event.turn == 1:
                loop.request_pause()

        kinds = [e.kind for e in events]
        assert AgentEventKind.SESSION_PAUSED in kinds
        # Should NOT have AGENT_DONE — we paused before turn 2
        assert AgentEventKind.AGENT_DONE not in kinds

        # Session should be marked PAUSED
        session = await store.get_session("s1")
        assert session is not None
        assert session.status == SessionStatus.PAUSED

    @pytest.mark.asyncio
    async def test_pause_completes_current_turn(self, tmp_path: Path) -> None:
        """Pause requested mid-stream still completes the current turn."""
        store = SQLiteEventStore(tmp_path / "test.db")
        spec = ToolSpec(
            name="slow", description="Slow", parameters={}, handler=lambda: "result"
        )
        # Turn 1: tool call → Turn 2: text → Turn 3: text (unreachable)
        backend = MockBackend([
            _make_tool_call_chunks("slow", {}),
            _make_text_chunks("second"),
            _make_text_chunks("third"),
        ])
        loop = AgentLoop(
            backend, _make_registry(spec), event_store=store, max_turns=10
        )

        events: list[AgentEvent] = []
        async for event in loop.run("go", session_id="s1"):
            events.append(event)
            # Request pause mid-stream (during first turn's TOOL_CALL)
            # The current turn should finish, then pause at boundary.
            if event.kind == AgentEventKind.TOOL_CALL:
                loop.request_pause()

        kinds = [e.kind for e in events]
        # Turn 1 should complete fully (TURN_START, TOOL_CALL, TURN_COMPLETE)
        # then SESSION_PAUSED emitted at boundary
        assert AgentEventKind.TURN_START in kinds
        assert AgentEventKind.TOOL_CALL in kinds
        assert AgentEventKind.TURN_COMPLETE in kinds
        assert AgentEventKind.SESSION_PAUSED in kinds
        # Should NOT have reached turn 2
        assert AgentEventKind.AGENT_DONE not in kinds


# ---------------------------------------------------------------------------
# Resume tests
# ---------------------------------------------------------------------------


class TestResume:
    @pytest.mark.asyncio
    async def test_resume_paused_session(self, tmp_path: Path) -> None:
        """Resume a paused session continues from where it left off."""
        store = SQLiteEventStore(tmp_path / "test.db")
        spec = ToolSpec(
            name="ping", description="Ping", parameters={}, handler=lambda: "pong"
        )
        # First run: tool call (turn 1) → paused
        backend1 = MockBackend([
            _make_tool_call_chunks("ping", {}),
            _make_text_chunks("unreachable"),
        ])
        loop1 = AgentLoop(
            backend1, _make_registry(spec), event_store=store, max_turns=5
        )

        events1: list[AgentEvent] = []
        async for event in loop1.run("go", session_id="s1"):
            events1.append(event)
            if event.kind == AgentEventKind.TURN_COMPLETE and event.turn == 1:
                loop1.request_pause()

        assert any(e.kind == AgentEventKind.SESSION_PAUSED for e in events1)

        # Now resume with a new backend that produces text
        backend2 = MockBackend([_make_text_chunks("resumed")])
        loop2 = AgentLoop(
            backend2, _make_registry(spec), event_store=store, max_turns=5
        )

        events2: list[AgentEvent] = []
        async for event in loop2.resume("s1"):
            events2.append(event)

        kinds2 = [e.kind for e in events2]
        assert AgentEventKind.AGENT_DONE in kinds2

        # Session should be COMPLETED now
        session = await store.get_session("s1")
        assert session is not None
        assert session.status == SessionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_resume_reconstructs_turn_number(self, tmp_path: Path) -> None:
        """Resume starts at the correct turn number."""
        store = SQLiteEventStore(tmp_path / "test.db")
        spec = ToolSpec(
            name="ping", description="Ping", parameters={}, handler=lambda: "pong"
        )
        backend1 = MockBackend([
            _make_tool_call_chunks("ping", {}),
            _make_text_chunks("not reached"),
        ])
        loop1 = AgentLoop(
            backend1, _make_registry(spec), event_store=store, max_turns=10
        )

        async for event in loop1.run("go", session_id="s1"):
            if event.kind == AgentEventKind.TURN_COMPLETE and event.turn == 1:
                loop1.request_pause()

        # Resume
        backend2 = MockBackend([_make_text_chunks("done")])
        loop2 = AgentLoop(
            backend2, _make_registry(spec), event_store=store, max_turns=10
        )

        events2: list[AgentEvent] = []
        async for event in loop2.resume("s1"):
            events2.append(event)

        # Turn should start at 2 (since turn 1 was completed before pause)
        turn_starts = [e for e in events2 if e.kind == AgentEventKind.TURN_START]
        assert len(turn_starts) >= 1
        assert turn_starts[0].turn == 2

    @pytest.mark.asyncio
    async def test_resume_non_paused_raises(self, tmp_path: Path) -> None:
        """Resuming a non-paused session raises ValueError."""
        store = SQLiteEventStore(tmp_path / "test.db")
        backend = MockBackend([_make_text_chunks("hello")])
        loop = AgentLoop(backend, _make_registry(), event_store=store)

        # Run to completion
        async for _ in loop.run("go", session_id="s1"):
            pass

        with pytest.raises(ValueError, match="not paused"):
            async for _ in loop.resume("s1"):
                pass

    @pytest.mark.asyncio
    async def test_resume_nonexistent_raises(self, tmp_path: Path) -> None:
        """Resuming a nonexistent session raises ValueError."""
        store = SQLiteEventStore(tmp_path / "test.db")
        backend = MockBackend([])
        loop = AgentLoop(backend, _make_registry(), event_store=store)

        with pytest.raises(ValueError, match="not found"):
            async for _ in loop.resume("nonexistent"):
                pass

    @pytest.mark.asyncio
    async def test_resume_without_store_raises(self) -> None:
        """Resuming without an event store raises RuntimeError."""
        backend = MockBackend([])
        loop = AgentLoop(backend, _make_registry())

        with pytest.raises(RuntimeError, match="event store"):
            async for _ in loop.resume("s1"):
                pass

    @pytest.mark.asyncio
    async def test_resume_with_explicit_prompt(self, tmp_path: Path) -> None:
        """Resume with an explicit prompt uses it instead of reconstructed."""
        store = SQLiteEventStore(tmp_path / "test.db")
        spec = ToolSpec(
            name="ping", description="Ping", parameters={}, handler=lambda: "pong"
        )
        backend1 = MockBackend([
            _make_tool_call_chunks("ping", {}),
            _make_text_chunks("not reached"),
        ])
        loop1 = AgentLoop(
            backend1, _make_registry(spec), event_store=store, max_turns=10
        )
        async for event in loop1.run("original", session_id="s1"):
            if event.kind == AgentEventKind.TURN_COMPLETE and event.turn == 1:
                loop1.request_pause()

        # Resume with override prompt — capture what the backend receives
        prompts_received: list[str] = []

        class CapturingBackend(MockBackend):
            @override
            async def stream(
                self, prompt: str, **kwargs: Any
            ) -> AsyncIterator[StreamChunk]:
                prompts_received.append(prompt)
                async for chunk in super().stream(prompt, **kwargs):
                    yield chunk

        backend2 = CapturingBackend([_make_text_chunks("done")])
        loop2 = AgentLoop(
            backend2, _make_registry(spec), event_store=store, max_turns=10
        )
        async for _ in loop2.resume("s1", prompt="new direction"):
            pass

        assert prompts_received[0] == "new direction"

    @pytest.mark.asyncio
    async def test_multiple_pause_resume_cycles(self, tmp_path: Path) -> None:
        """Multiple pause/resume cycles work correctly."""
        store = SQLiteEventStore(tmp_path / "test.db")
        spec = ToolSpec(
            name="ping", description="Ping", parameters={}, handler=lambda: "pong"
        )

        # Cycle 1: tool call → pause
        backend1 = MockBackend([
            _make_tool_call_chunks("ping", {}),
            _make_text_chunks("not reached"),
        ])
        loop1 = AgentLoop(
            backend1, _make_registry(spec), event_store=store, max_turns=10
        )
        async for event in loop1.run("start", session_id="s1"):
            if event.kind == AgentEventKind.TURN_COMPLETE and event.turn == 1:
                loop1.request_pause()

        session = await store.get_session("s1")
        assert session is not None
        assert session.status == SessionStatus.PAUSED

        # Cycle 2: resume → tool call → pause again
        backend2 = MockBackend([
            _make_tool_call_chunks("ping", {}),
            _make_text_chunks("not reached"),
        ])
        loop2 = AgentLoop(
            backend2, _make_registry(spec), event_store=store, max_turns=10
        )

        events2: list[AgentEvent] = []
        async for event in loop2.resume("s1"):
            events2.append(event)
            if event.kind == AgentEventKind.TURN_COMPLETE and event.turn == 2:
                loop2.request_pause()

        assert any(e.kind == AgentEventKind.SESSION_PAUSED for e in events2)
        session = await store.get_session("s1")
        assert session is not None
        assert session.status == SessionStatus.PAUSED

        # Cycle 3: resume → complete
        backend3 = MockBackend([_make_text_chunks("final")])
        loop3 = AgentLoop(
            backend3, _make_registry(spec), event_store=store, max_turns=10
        )

        events3: list[AgentEvent] = []
        async for event in loop3.resume("s1"):
            events3.append(event)

        assert any(e.kind == AgentEventKind.AGENT_DONE for e in events3)
        session = await store.get_session("s1")
        assert session is not None
        assert session.status == SessionStatus.COMPLETED


# ---------------------------------------------------------------------------
# User input injection tests
# ---------------------------------------------------------------------------


class TestUserInput:
    @pytest.mark.asyncio
    async def test_inject_user_input(self, tmp_path: Path) -> None:
        """Injected user input becomes a USER_INPUT event and next prompt."""
        store = SQLiteEventStore(tmp_path / "test.db")
        spec = ToolSpec(
            name="ping", description="Ping", parameters={}, handler=lambda: "pong"
        )

        prompts_received: list[str] = []

        class CapturingBackend(MockBackend):
            @override
            async def stream(
                self, prompt: str, **kwargs: Any
            ) -> AsyncIterator[StreamChunk]:
                prompts_received.append(prompt)
                async for chunk in super().stream(prompt, **kwargs):
                    yield chunk

        # Turn 1: tool call → Turn 2 (user input injected) → Turn 3: text done
        backend = CapturingBackend([
            _make_tool_call_chunks("ping", {}),
            _make_text_chunks("after injection"),
            _make_text_chunks("not reached"),
        ])
        loop = AgentLoop(
            backend, _make_registry(spec), event_store=store, max_turns=10
        )

        events: list[AgentEvent] = []
        async for event in loop.run("initial", session_id="s1"):
            events.append(event)
            # Inject user input after first tool call turn completes
            if event.kind == AgentEventKind.TURN_COMPLETE and event.turn == 1:
                loop.inject_user_input("user says hi")

        kinds = [e.kind for e in events]
        assert AgentEventKind.USER_INPUT in kinds
        # The user input text should be in the event
        user_events = [e for e in events if e.kind == AgentEventKind.USER_INPUT]
        assert len(user_events) == 1
        assert user_events[0].text == "user says hi"
        # The prompt after injection should be the user's text
        assert "user says hi" in prompts_received

    @pytest.mark.asyncio
    async def test_user_input_persisted(self, tmp_path: Path) -> None:
        """USER_INPUT events are persisted to the event store."""
        store = SQLiteEventStore(tmp_path / "test.db")
        spec = ToolSpec(
            name="ping", description="Ping", parameters={}, handler=lambda: "pong"
        )
        backend = MockBackend([
            _make_tool_call_chunks("ping", {}),
            _make_text_chunks("done"),
        ])
        loop = AgentLoop(
            backend, _make_registry(spec), event_store=store, max_turns=10
        )

        async for event in loop.run("go", session_id="s1"):
            if event.kind == AgentEventKind.TURN_COMPLETE and event.turn == 1:
                loop.inject_user_input("injected")

        stored = await store.get_events("s1")
        user_inputs = [
            e for e in stored if e.kind == AgentEventKind.USER_INPUT
        ]
        assert len(user_inputs) == 1
        assert user_inputs[0].payload.get("text") == "injected"


# ---------------------------------------------------------------------------
# State reconstruction tests
# ---------------------------------------------------------------------------


class TestReconstructState:
    @pytest.mark.asyncio
    async def test_reconstruct_accumulated_text(self, tmp_path: Path) -> None:
        """State reconstruction recovers turn number and text from tool turns."""
        store = SQLiteEventStore(tmp_path / "test.db")
        spec = ToolSpec(
            name="ping", description="Ping", parameters={}, handler=lambda: "pong"
        )
        # Turn 1: tool call → tool executed → turn 2 text → pause
        backend = MockBackend([
            _make_tool_call_chunks("ping", {}),
            _make_text_chunks("hello world"),
            _make_text_chunks("not reached"),
        ])
        loop = AgentLoop(
            backend, _make_registry(spec), event_store=store, max_turns=10
        )
        async for event in loop.run("go", session_id="s1"):
            # Pause after turn 2 (text turn) so we have tool results + text
            if event.kind == AgentEventKind.TURN_COMPLETE and event.turn == 2:
                loop.request_pause()

        events = await store.get_events("s1")
        turn, acc_text, messages, _last_prompt = AgentLoop.reconstruct_state(events)
        assert turn == 2
        # Accumulated text should contain the text from turn 2
        assert "hello" in acc_text
        # The tool results from turn 1 are captured in messages
        assert len(messages) >= 2  # assistant + tool_result from turn 1

    @pytest.mark.asyncio
    async def test_reconstruct_tool_messages(self, tmp_path: Path) -> None:
        """State reconstruction recovers structured tool messages."""
        store = SQLiteEventStore(tmp_path / "test.db")
        spec = ToolSpec(
            name="echo",
            description="Echo",
            parameters={"type": "object", "properties": {"msg": {"type": "string"}}},
            handler=_echo_handler,
        )
        # Turn 1: tool call → tool executed → Turn 2: text → pause
        # Pausing after turn 2 ensures turn 1's TOOL_RESULT is in the store.
        backend = MockBackend([
            _make_tool_call_chunks("echo", {"msg": "test"}),
            _make_text_chunks("after tools"),
            _make_text_chunks("not reached"),
        ])
        loop = AgentLoop(
            backend, _make_registry(spec), event_store=store, max_turns=10
        )
        async for event in loop.run("go", session_id="s1"):
            if event.kind == AgentEventKind.TURN_COMPLETE and event.turn == 2:
                loop.request_pause()

        events = await store.get_events("s1")
        turn, _, messages, _ = AgentLoop.reconstruct_state(events)
        assert turn == 2
        # Should have assistant message + tool result message from turn 1
        assert len(messages) >= 2
        assert messages[0].role == Role.ASSISTANT
        assert messages[1].role == Role.TOOL_RESULT


# ---------------------------------------------------------------------------
# TOOL_CALL emission timing tests
# ---------------------------------------------------------------------------


class TestToolCallTiming:
    @pytest.mark.asyncio
    async def test_tool_call_includes_input(self, tmp_path: Path) -> None:
        """TOOL_CALL events include full tool_input (not emitted at TOOL_USE_START)."""
        store = SQLiteEventStore(tmp_path / "test.db")
        spec = ToolSpec(
            name="echo",
            description="Echo",
            parameters={"type": "object", "properties": {"msg": {"type": "string"}}},
            handler=_echo_handler,
        )
        backend = MockBackend([
            _make_tool_call_chunks("echo", {"msg": "hello"}),
            _make_text_chunks("done"),
        ])
        loop = AgentLoop(
            backend, _make_registry(spec), event_store=store, max_turns=5
        )

        events: list[AgentEvent] = []
        async for event in loop.run("go", session_id="s1"):
            events.append(event)

        tool_calls = [e for e in events if e.kind == AgentEventKind.TOOL_CALL]
        assert len(tool_calls) == 1
        assert tool_calls[0].tool_name == "echo"
        assert tool_calls[0].tool_input == {"msg": "hello"}

    @pytest.mark.asyncio
    async def test_tool_call_persisted_with_input(self, tmp_path: Path) -> None:
        """Persisted TOOL_CALL events include full tool_input."""
        store = SQLiteEventStore(tmp_path / "test.db")
        spec = ToolSpec(
            name="echo",
            description="Echo",
            parameters={"type": "object", "properties": {"msg": {"type": "string"}}},
            handler=_echo_handler,
        )
        backend = MockBackend([
            _make_tool_call_chunks("echo", {"msg": "world"}),
            _make_text_chunks("done"),
        ])
        loop = AgentLoop(
            backend, _make_registry(spec), event_store=store, max_turns=5
        )
        async for _ in loop.run("go", session_id="s1"):
            pass

        stored = await store.get_events("s1")
        tool_calls = [e for e in stored if e.kind == AgentEventKind.TOOL_CALL]
        assert len(tool_calls) == 1
        assert tool_calls[0].payload.get("tool_input") == {"msg": "world"}
