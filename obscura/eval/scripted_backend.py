"""scripted_backend — deterministic ``BackendProtocol`` for offline evals.

A real LLM backend is overkill for evaluating that the *tools* work and
that the agent loop's plumbing routes events correctly.  This backend
takes a fixed script of "turns" — each turn is a list of tool calls
followed by an optional final text — and replays it as ``StreamChunk``
events.

Why not reuse one of the eval_backend.py implementations?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Both ``AnthropicEvalBackend`` and ``ClaudeCliEvalBackend`` make real
network calls / subprocess invocations. We want eval cases to be:

* Deterministic — same inputs → same outputs every run.
* CI-friendly — no API keys, no network, no installed CLI.
* Free — runs in <100ms.

A scripted backend gives us all three. It's intentionally minimal —
just enough surface for ``EvalEngine.run_case`` to drive it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    Backend,
    BackendCapabilities,
    ChunkKind,
    ContentBlock,
    HookPoint,
    Message,
    NativeHandle,
    Role,
    SessionRef,
    StreamChunk,
    StreamMetadata,
    ToolSpec,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


# ---------------------------------------------------------------------------
# Script primitives
# ---------------------------------------------------------------------------


@dataclass
class ScriptedToolCall:
    """A single tool invocation in a scripted turn."""

    name: str
    input: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass
class ScriptedTurn:
    """One model-side turn: zero or more tool calls + optional final text.

    The agent loop drives multiple turns until the model emits no tool
    calls. So a typical script is::

        [
            ScriptedTurn(tool_calls=[ScriptedToolCall("browser_read_page")]),
            ScriptedTurn(text="The page title is ..."),
        ]
    """

    tool_calls: list[ScriptedToolCall] = field(default_factory=list[ScriptedToolCall])
    text: str = ""


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class ScriptedBackend:
    """A canned-response ``BackendProtocol`` for deterministic eval runs.

    The first ``stream()`` call emits the first turn, the next call
    emits the second turn, and so on. After the script is exhausted,
    further calls emit only an empty final turn (no tools, empty text)
    so the agent loop terminates cleanly.

    ``planner`` lets the script branch on tool results — useful for
    cheap-vs-CDP escalation tests where the next turn depends on
    whether the previous tool call returned a successful mutation.
    """

    def __init__(
        self,
        script: list[ScriptedTurn] | None = None,
        *,
        planner: Any = None,
        model: str = "scripted-eval",
    ) -> None:
        self._script: list[ScriptedTurn] = list(script or [])
        self._planner = planner
        self._model = model
        self._registry = ToolRegistry()
        self._hooks: dict[HookPoint, list[Any]] = {hp: [] for hp in HookPoint}
        self._turn_index = 0
        # Tool results from the previous turn — the planner can read these to
        # decide what to do next. The agent loop feeds tool results back as
        # part of the next ``stream()`` call's prompt argument; we capture
        # them by registering tool wrappers.
        self._last_tool_results: list[dict[str, Any]] = []

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    # -- streaming -----------------------------------------------------------

    async def stream(  # noqa: C901
        self,
        prompt: str,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        del kwargs  # not used
        # Determine the turn to emit. Planner takes precedence; otherwise
        # we walk the script in order.
        turn = self._next_turn(prompt)

        yield StreamChunk(kind=ChunkKind.MESSAGE_START)

        # Emit text delta first so the eval can capture output_text.
        if turn.text:
            yield StreamChunk(kind=ChunkKind.TEXT_DELTA, text=turn.text)

        # Then tool calls. The agent loop will execute each in turn and
        # feed results back via the next stream() call.
        for tc in turn.tool_calls:
            tid = f"toolu_{uuid.uuid4().hex[:10]}"
            yield StreamChunk(
                kind=ChunkKind.TOOL_USE_START,
                tool_name=tc.name,
                tool_use_id=tid,
            )
            yield StreamChunk(
                kind=ChunkKind.TOOL_USE_DELTA,
                tool_use_id=tid,
                tool_input_delta=json.dumps(tc.input),
            )
            yield StreamChunk(
                kind=ChunkKind.TOOL_USE_END,
                tool_name=tc.name,
                tool_use_id=tid,
                tool_input_delta=json.dumps(tc.input),
            )

        yield StreamChunk(
            kind=ChunkKind.DONE,
            metadata=StreamMetadata(model_id=self._model, finish_reason="end_turn"),
        )

    def _next_turn(self, prompt: str) -> ScriptedTurn:
        """Resolve the next scripted turn.

        Calls the planner if one was provided; otherwise advances the
        static script. Returns an empty terminator turn if both are
        exhausted, which lets the agent loop finish cleanly.
        """
        if self._planner is not None:
            try:
                planned = self._planner(prompt, self._turn_index)
            except Exception:
                planned = None
            if planned is not None:
                self._turn_index += 1
                return planned

        if self._turn_index < len(self._script):
            turn = self._script[self._turn_index]
            self._turn_index += 1
            return turn

        # Exhausted — terminator
        return ScriptedTurn()

    # -- send (used only by judge backend, not exercised here) ---------------

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        text_parts: list[str] = []
        async for chunk in self.stream(prompt, **kwargs):
            if chunk.kind == ChunkKind.TEXT_DELTA:
                text_parts.append(chunk.text)
        return Message(
            role=Role.ASSISTANT,
            content=[ContentBlock(kind="text", text="".join(text_parts))],
            model=self._model,
        )

    # -- sessions (stub) -----------------------------------------------------

    async def create_session(self, **kwargs: Any) -> SessionRef:
        del kwargs
        return SessionRef(
            session_id=f"scripted-{uuid.uuid4().hex[:12]}",
            backend=Backend.CLAUDE,
        )

    async def resume_session(self, ref: SessionRef) -> None:
        del ref

    async def list_sessions(self) -> list[SessionRef]:
        return []

    async def delete_session(self, ref: SessionRef) -> None:
        del ref

    # -- tools & hooks -------------------------------------------------------

    def register_tool(self, spec: ToolSpec) -> None:
        if spec.name not in {s.name for s in self._registry.all()}:
            self._registry.register(spec)

    def register_hook(self, hook: HookPoint, callback: Any) -> None:
        self._hooks[hook].append(callback)

    def get_tool_registry(self) -> ToolRegistry:
        return self._registry

    # -- metadata ------------------------------------------------------------

    @property
    def native(self) -> NativeHandle:
        return NativeHandle()

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_calls=True,
        )


__all__ = [
    "ScriptedBackend",
    "ScriptedToolCall",
    "ScriptedTurn",
]
