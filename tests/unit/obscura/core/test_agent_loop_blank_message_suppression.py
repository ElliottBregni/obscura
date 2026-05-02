"""End-to-end integration test for stream-time blank-message suppression.

These tests prove that ``ContinuationTextSuppressor`` is wired into the
agent loop's per-turn streaming and actually prevents the offending text
from reaching the consumer — not just that the unit-test class exists.

Setup uses the same ``MockBackend`` + ``_make_*_chunks`` pattern as the
hooks integration tests, with a 2-turn scenario:

* Turn 1: user prompt "hi" → model emits a tool call → tool returns
* Turn 2: continuation (``current_prompt == ""``) → model streams the
  blank-message hallucination

After turn 2 we assert (a) no TEXT_DELTA events contain the offending
phrase, and (b) the harness cue was queued for the next continuation —
proving the post-suppression handling fires too.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from obscura.core.agent_loop import AgentLoop
from obscura.core.tools import ToolRegistry
from obscura.core.types import (
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

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


def _text_chunks(text: str) -> list[StreamChunk]:
    """Split *text* into ~6-char chunks. Forces the suppressor to
    re-scan after each delta, exercising the streaming-buffer logic
    rather than the trivial "single chunk has the whole phrase" path."""
    chunks: list[StreamChunk] = []
    step = 6
    for i in range(0, len(text), step):
        chunks.append(StreamChunk(kind=ChunkKind.TEXT_DELTA, text=text[i : i + step]))
    chunks.append(StreamChunk(kind=ChunkKind.DONE))
    return chunks


def _tool_call_chunks(name: str, tool_input: dict[str, Any]) -> list[StreamChunk]:
    return [
        StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name=name),
        StreamChunk(
            kind=ChunkKind.TOOL_USE_DELTA,
            tool_input_delta=json.dumps(tool_input),
        ),
        StreamChunk(kind=ChunkKind.TOOL_USE_END),
        StreamChunk(kind=ChunkKind.DONE),
    ]


class _Backend:
    def __init__(self, turns: list[list[StreamChunk]]) -> None:
        self._turns = list(turns)
        self._i = 0
        self._registry = ToolRegistry()

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        chunks = (
            self._turns[self._i]
            if self._i < len(self._turns)
            else [StreamChunk(kind=ChunkKind.DONE)]
        )
        self._i += 1
        for c in chunks:
            yield c

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

    def register_hook(
        self, hook: HookPoint, callback: Callable[..., Any]
    ) -> None:
        return None

    def get_tool_registry(self) -> ToolRegistry:
        return self._registry

    @property
    def native(self) -> NativeHandle:
        return NativeHandle()

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()


def _registry_with_ping() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="ping",
            description="Ping",
            parameters={},
            handler=lambda: "pong",
        )
    )
    return reg


_BLANK_PHRASE = (
    "Looks like your message came in blank — did you mean to send something?"
)


class TestStreamTimeSuppressionEndToEnd:
    @pytest.mark.asyncio
    async def test_blank_message_text_never_reaches_consumer(self) -> None:
        """The phrase the model produced on a continuation turn must not
        appear in any TEXT_DELTA event yielded to the agent loop's
        consumer (i.e. the user)."""
        backend = _Backend(
            [
                _tool_call_chunks("ping", {}),  # turn 1: tool call
                _text_chunks(_BLANK_PHRASE),  # turn 2: hallucination
            ]
        )
        loop = AgentLoop(backend, _registry_with_ping())

        events = [e async for e in loop.run("hi")]

        text_deltas = [
            (e.text or "")
            for e in events
            if e.kind == AgentEventKind.TEXT_DELTA
        ]
        joined = "".join(text_deltas)
        # The whole offending phrase must be gone.
        assert "came in blank" not in joined
        assert "did you mean to send" not in joined
        # The prefix that was already buffered when the pattern fired
        # must also be gone (the suppressor drops the buffer, not just
        # text after the trigger point).
        assert "Looks like" not in joined

    @pytest.mark.asyncio
    async def test_harness_cue_queued_after_suppression(self) -> None:
        """The next continuation should get the harness-tagged cue so
        the model's server-side history (which still contains the
        offending text via Copilot's session state) gets corrected."""
        backend = _Backend(
            [
                _tool_call_chunks("ping", {}),
                _text_chunks(_BLANK_PHRASE),
            ]
        )
        loop = AgentLoop(backend, _registry_with_ping())

        [e async for e in loop.run("hi")]

        # Cue queued, ready for the next continuation to consume.
        assert loop._pending_blank_msg_cue is not None
        assert "[internal:obscura-harness]" in loop._pending_blank_msg_cue
        assert loop._pending_correction is not None

    @pytest.mark.asyncio
    async def test_first_turn_text_passes_through(self) -> None:
        """Suppression is only active on continuation turns. On the
        first turn (real user prompt) the same hallucinated text — if
        the model somehow produced it — would pass through. Regression
        guard against accidentally activating the suppressor on
        user-driven turns."""
        backend = _Backend([_text_chunks(_BLANK_PHRASE)])
        loop = AgentLoop(backend, ToolRegistry())

        events = [e async for e in loop.run("hi")]

        joined = "".join(
            (e.text or "")
            for e in events
            if e.kind == AgentEventKind.TEXT_DELTA
        )
        # The first turn is user-driven (current_prompt = "hi"), so
        # the suppressor is constructed inactive and text passes through.
        assert "came in blank" in joined

    @pytest.mark.asyncio
    async def test_short_continuation_response_not_swallowed(self) -> None:
        """A short, *legitimate* response on a continuation turn (under
        the suppression window, no blank-message pattern) must be
        flushed by ``finalize()`` at end-of-stream, not silently
        dropped."""
        backend = _Backend(
            [
                _tool_call_chunks("ping", {}),
                _text_chunks("got it"),  # 6 chars, well under window
            ]
        )
        loop = AgentLoop(backend, _registry_with_ping())

        events = [e async for e in loop.run("hi")]

        joined = "".join(
            (e.text or "")
            for e in events
            if e.kind == AgentEventKind.TEXT_DELTA
        )
        assert "got it" in joined
        # No suppression should have fired on legitimate text.
        assert loop._pending_blank_msg_cue is None
