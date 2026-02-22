"""MockBackend + MockBackendBuilder — reusable backend mocks for tests.

Consolidates the ``MockBackend`` class that was copy-pasted across test
files and adds a fluent builder so new tests can create mocks in < 10 lines.

Usage::

    from obscura.testing import MockBackendBuilder, text_chunks, tool_call_chunks

    backend = (
        MockBackendBuilder()
        .with_turn(text_chunks("Hello!"))
        .with_turn(
            tool_call_chunks("search", {"q": "weather"})
            + text_chunks("72 degrees")
        )
        .build()
    )

    # Use with AgentLoop
    loop = AgentLoop(backend, backend.get_tool_registry())
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable, override

from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    Backend,
    BackendCapabilities,
    BackendProtocol,
    ChunkKind,
    HookPoint,
    Message,
    NativeHandle,
    Role,
    SessionRef,
    StreamChunk,
    ToolSpec,
)

__all__ = ["MockBackend", "MockBackendBuilder"]


# ---------------------------------------------------------------------------
# MockBackend
# ---------------------------------------------------------------------------


class MockBackend(BackendProtocol):
    """A mock backend that returns pre-configured stream responses per turn.

    This is the canonical test mock that implements :class:`BackendProtocol`.
    It replays a list of ``StreamChunk`` sequences — one per LLM "turn" —
    and falls back to a single ``DONE`` chunk when the sequence is exhausted.

    Attributes:
        call_count: Number of ``stream()`` invocations so far.
        prompts: List of prompts received (for assertions).
    """

    def __init__(
        self,
        turn_responses: list[list[StreamChunk]] | None = None,
    ) -> None:
        self._turns: list[list[StreamChunk]] = list(turn_responses) if turn_responses else []
        self.call_count: int = 0
        self.prompts: list[str] = []
        self._registry = ToolRegistry()
        self._hooks: dict[HookPoint, list[Callable[..., Any]]] = {hp: [] for hp in HookPoint}

    # -- BackendProtocol implementation ------------------------------------

    @override
    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        self.prompts.append(prompt)
        if self.call_count < len(self._turns):
            chunks = self._turns[self.call_count]
        else:
            chunks = [StreamChunk(kind=ChunkKind.DONE)]
        self.call_count += 1
        for chunk in chunks:
            yield chunk

    @override
    async def start(self) -> None:
        return None

    @override
    async def stop(self) -> None:
        return None

    @override
    async def send(self, prompt: str, **kwargs: Any) -> Message:
        self.prompts.append(prompt)
        return Message(role=Role.ASSISTANT, content=[], raw=None)

    @override
    async def create_session(self, **kwargs: Any) -> SessionRef:
        return SessionRef(session_id="mock-sess", backend=Backend.COPILOT)

    @override
    async def resume_session(self, ref: SessionRef) -> None:
        return None

    @override
    async def list_sessions(self) -> list[SessionRef]:
        return []

    @override
    async def delete_session(self, ref: SessionRef) -> None:
        return None

    @override
    def register_tool(self, spec: ToolSpec) -> None:
        self._registry.register(spec)

    @override
    def register_hook(self, hook: HookPoint, callback: Callable[..., Any]) -> None:
        self._hooks.setdefault(hook, []).append(callback)

    @override
    def get_tool_registry(self) -> ToolRegistry:
        return self._registry

    @property
    @override
    def native(self) -> NativeHandle:
        return NativeHandle()

    @override
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()


# ---------------------------------------------------------------------------
# MockBackendBuilder  (fluent API)
# ---------------------------------------------------------------------------


class MockBackendBuilder:
    """Fluent builder for constructing :class:`MockBackend` instances.

    Usage::

        backend = (
            MockBackendBuilder()
            .with_turn(text_chunks("Hello!"))
            .with_turn(tool_call_chunks("read", {"path": "a.py"}))
            .with_turn(text_chunks("Done."))
            .with_tool(make_tool("read", handler=my_handler))
            .build()
        )
    """

    def __init__(self) -> None:
        self._turns: list[list[StreamChunk]] = []
        self._tools: list[ToolSpec] = []

    def with_turn(self, chunks: list[StreamChunk]) -> MockBackendBuilder:
        """Add a turn response (list of StreamChunks the mock will yield)."""
        self._turns.append(chunks)
        return self

    def with_turns(self, *turns: list[StreamChunk]) -> MockBackendBuilder:
        """Add multiple turn responses at once."""
        for turn in turns:
            self._turns.append(turn)
        return self

    def with_text(self, text: str) -> MockBackendBuilder:
        """Shorthand: add a turn that streams *text* word-by-word + DONE.

        Equivalent to ``with_turn(text_chunks(text))``.
        """
        from obscura.testing.chunks import text_chunks

        self._turns.append(text_chunks(text))
        return self

    def with_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        *,
        preceding_text: str = "",
    ) -> MockBackendBuilder:
        """Shorthand: add a turn where the model calls a tool.

        Equivalent to ``with_turn(tool_call_chunks(...))``.
        """
        from obscura.testing.chunks import tool_call_chunks

        self._turns.append(
            tool_call_chunks(tool_name, tool_input, preceding_text=preceding_text)
        )
        return self

    def with_tool(self, spec: ToolSpec) -> MockBackendBuilder:
        """Register a tool with the resulting backend."""
        self._tools.append(spec)
        return self

    def with_tools(self, *specs: ToolSpec) -> MockBackendBuilder:
        """Register multiple tools."""
        self._tools.extend(specs)
        return self

    def build(self) -> MockBackend:
        """Build the :class:`MockBackend`."""
        backend = MockBackend(self._turns)
        for spec in self._tools:
            backend.register_tool(spec)
        return backend
