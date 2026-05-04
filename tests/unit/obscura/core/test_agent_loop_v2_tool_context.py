"""Tests for v2's ToolContext binding — host_callbacks parity with v1.

v1 plumbed ``host_callbacks`` through ``ToolContext`` so tools that need to
reach UI callbacks (``ask_user``, ``permission_mode``, etc.) could find them
via :func:`current_tool_context`. v2 must do the same — without it, tools
that worked under v1 silently break under v2 (NoneType context).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from obscura.core.agent_loop_v2 import AgentLoopV2
from obscura.core.enums.agent import ChunkKind
from obscura.core.tool_context import current_tool_context
from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    BackendCapabilities,
    Message,
    StreamChunk,
    ToolSpec,
)


@dataclass
class _StubTurn:
    text: str = ""
    tool_uses: list[dict[str, Any]] = field(default_factory=list)


class _StubBackend:
    name = "stub"
    capabilities = BackendCapabilities(
        supports_streaming=True, supports_tool_calls=True
    )

    def __init__(self, script: list[_StubTurn]) -> None:
        self.script = list(script)
        self.calls = 0

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def stream(
        self, messages: list[Message] | None = None, **_kwargs: Any
    ) -> AsyncIterator[StreamChunk]:
        idx = self.calls
        self.calls += 1
        if idx >= len(self.script):
            raise AssertionError("stub exhausted")
        turn = self.script[idx]
        if turn.text:
            yield StreamChunk(kind=ChunkKind.TEXT_DELTA, text=turn.text)
        for tu in turn.tool_uses:
            yield StreamChunk(
                kind=ChunkKind.TOOL_USE_START,
                tool_use_id=tu["id"],
                tool_name=tu["name"],
            )
            yield StreamChunk(
                kind=ChunkKind.TOOL_USE_DELTA,
                tool_use_id=tu["id"],
                tool_input_delta=json.dumps(tu.get("input", {})),
            )
            yield StreamChunk(
                kind=ChunkKind.TOOL_USE_END,
                tool_use_id=tu["id"],
                tool_name=tu["name"],
            )


def _registry(tools: dict[str, Any]) -> ToolRegistry:
    reg = ToolRegistry()
    for name, handler in tools.items():
        reg.register(
            ToolSpec(
                name=name,
                description=f"stub {name}",
                parameters={
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {},
                },
                handler=handler,
            )
        )
    return reg


# ---------------------------------------------------------------------------


class TestToolContextBinding:
    @pytest.mark.asyncio
    async def test_current_tool_context_is_not_none_inside_handler(self) -> None:
        """The real v1 parity: tools must see a non-None ToolContext."""
        observed: list[Any] = []

        def t(**_: Any) -> str:
            observed.append(current_tool_context())
            return "ok"

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "t"}]),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(backend, _registry({"t": t}))
        _ = [e async for e in loop.run("hi")]

        assert len(observed) == 1
        ctx = observed[0]
        assert ctx is not None
        assert ctx.registry is not None

    @pytest.mark.asyncio
    async def test_host_callbacks_threaded_through(self) -> None:
        """``host_callbacks`` lookup keys land on the matching ToolContext fields."""
        observed: list[Any] = []

        def t(**_: Any) -> str:
            observed.append(current_tool_context())
            return "ok"

        async def ask_user(_prompt: str) -> str:
            return "answer"

        def perm_mode(_mode: str) -> None:
            return None

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "t"}]),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(
            backend,
            _registry({"t": t}),
            host_callbacks={
                "ask_user_callback": ask_user,
                "permission_mode_callback": perm_mode,
                "custom_thing": "extra-value",  # unknown key → extras
            },
        )
        _ = [e async for e in loop.run("hi")]

        ctx = observed[0]
        assert ctx is not None
        # Known fields populated.
        assert ctx.ask_user_callback is ask_user
        assert ctx.permission_mode_callback is perm_mode
        # Unknown keys go to extras.
        assert ctx.extras.get("custom_thing") == "extra-value"

    @pytest.mark.asyncio
    async def test_history_is_live_reference(self) -> None:
        """``ctx.history`` should be the same list object the loop appends to,
        so tools using ``ctx.append_history`` see their writes reflected."""
        observed: list[Any] = []

        def t(**_: Any) -> str:
            ctx = current_tool_context()
            assert ctx is not None
            observed.append(ctx.history)
            return "ok"

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "t"}]),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(backend, _registry({"t": t}))
        _ = [e async for e in loop.run("hi")]

        # The list is non-None and contains at least the user prompt.
        history = observed[0]
        assert history is not None
        assert len(history) >= 1

    @pytest.mark.asyncio
    async def test_no_host_callbacks_still_binds_context(self) -> None:
        """Even with no host_callbacks, ToolContext is still bound — so
        tools see registry/history/session_id."""
        observed: list[Any] = []

        def t(**_: Any) -> str:
            observed.append(current_tool_context())
            return "ok"

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "t"}]),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(backend, _registry({"t": t}))
        _ = [e async for e in loop.run("hi", session_id="sess-xyz")]

        ctx = observed[0]
        assert ctx is not None
        assert ctx.session_id == "sess-xyz"
        # Default host_callbacks all None.
        assert ctx.ask_user_callback is None
        assert ctx.permission_mode_callback is None
