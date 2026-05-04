"""Tests for the v1→v2 dispatch middleware ports.

Each middleware is exercised against AgentLoopV2 with a stub backend so
we verify integration end-to-end, not just the wrapper in isolation.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from obscura.core.agent_loop_middleware import (
    capability_gate,
    hook_middleware,
    tool_allowlist,
    tool_confirmation,
    tool_output_level,
)
from obscura.core.agent_loop_v2 import AgentLoopV2
from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    AgentEventKind,
    BackendCapabilities,
    ChunkKind,
    Message,
    StreamChunk,
    ToolSpec,
)


# ---------------------------------------------------------------------------
# Stub backend (re-used pattern from test_agent_loop_v2)
# ---------------------------------------------------------------------------


@dataclass
class _StubTurn:
    text: str = ""
    tool_uses: list[dict[str, Any]] = field(default_factory=list)


class _StubBackend:
    name = "stub"
    capabilities = BackendCapabilities(
        supports_streaming=True,
        supports_tool_calls=True,
    )

    def __init__(self, script: list[_StubTurn]) -> None:
        self.script = list(script)
        self.calls = 0
        self.received_messages: list[list[Message]] = []

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def stream(
        self, messages: list[Message] | None = None, **_kwargs: Any
    ) -> AsyncIterator[StreamChunk]:
        self.received_messages.append(list(messages or []))
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
# capability_gate
# ---------------------------------------------------------------------------


class TestCapabilityGate:
    @pytest.mark.asyncio
    async def test_denied_call_returns_error_block_does_not_invoke_handler(
        self,
    ) -> None:
        invoked: list[str] = []

        def t(**_: Any) -> str:
            invoked.append("yes")
            return "ok"

        class _Token:
            def allows(self, name: str) -> bool:
                return name != "forbidden_tool"

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "forbidden_tool"}]),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(
            backend,
            _registry({"forbidden_tool": t}),
            dispatch_middleware=[capability_gate(_Token())],
        )
        events = [e async for e in loop.run("call forbidden")]

        # Handler not invoked — gate denied.
        assert invoked == []
        # The tool_result block carries the denial text and is_error=True.
        last_user_msg = backend.received_messages[1][-1]
        result_block = next(b for b in last_user_msg.content if b.kind == "tool_result")
        assert result_block.is_error
        assert "Capability denied" in result_block.text
        # Loop completes cleanly.
        kinds = [e.kind for e in events]
        assert AgentEventKind.AGENT_DONE in kinds

    @pytest.mark.asyncio
    async def test_allowed_call_passes_through(self) -> None:
        invoked: list[str] = []

        def t(**_: Any) -> str:
            invoked.append("yes")
            return "ok"

        class _Token:
            def allows(self, _name: str) -> bool:
                return True

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "ok_tool"}]),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(
            backend,
            _registry({"ok_tool": t}),
            dispatch_middleware=[capability_gate(_Token())],
        )
        _ = [e async for e in loop.run("ok")]
        assert invoked == ["yes"]

    @pytest.mark.asyncio
    async def test_token_without_allows_method_is_permissive(self) -> None:
        invoked: list[str] = []

        def t(**_: Any) -> str:
            invoked.append("yes")
            return "ok"

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "t"}]),
                _StubTurn(text="done"),
            ]
        )
        # Token is a bare object with no allows/is_authorized — should
        # be treated as permissive (no-op gate).
        loop = AgentLoopV2(
            backend,
            _registry({"t": t}),
            dispatch_middleware=[capability_gate(object())],
        )
        _ = [e async for e in loop.run("ok")]
        assert invoked == ["yes"]


# ---------------------------------------------------------------------------
# tool_allowlist
# ---------------------------------------------------------------------------


class TestToolAllowlist:
    @pytest.mark.asyncio
    async def test_blocks_tool_not_in_allowlist(self) -> None:
        invoked: list[str] = []

        def blocked(**_: Any) -> str:
            invoked.append("blocked")
            return "x"

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "blocked"}]),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(
            backend,
            _registry({"blocked": blocked}),
            dispatch_middleware=[tool_allowlist(["allowed"])],
        )
        _ = [e async for e in loop.run("test")]
        assert invoked == []

    @pytest.mark.asyncio
    async def test_allows_tool_in_allowlist(self) -> None:
        invoked: list[str] = []

        def allowed(**_: Any) -> str:
            invoked.append("allowed")
            return "x"

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "allowed"}]),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(
            backend,
            _registry({"allowed": allowed}),
            dispatch_middleware=[tool_allowlist(["allowed", "other"])],
        )
        _ = [e async for e in loop.run("test")]
        assert invoked == ["allowed"]


# ---------------------------------------------------------------------------
# tool_confirmation
# ---------------------------------------------------------------------------


class TestToolConfirmation:
    @pytest.mark.asyncio
    async def test_rejection_blocks_handler(self) -> None:
        invoked: list[str] = []

        def t(**_: Any) -> str:
            invoked.append("yes")
            return "ok"

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "t"}]),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(
            backend,
            _registry({"t": t}),
            dispatch_middleware=[tool_confirmation(lambda _node: False)],
        )
        _ = [e async for e in loop.run("test")]
        assert invoked == []

    @pytest.mark.asyncio
    async def test_async_callback_supported(self) -> None:
        invoked: list[str] = []

        def t(**_: Any) -> str:
            invoked.append("yes")
            return "ok"

        async def confirm(_node: Any) -> bool:
            return True

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "t"}]),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(
            backend,
            _registry({"t": t}),
            dispatch_middleware=[tool_confirmation(confirm)],
        )
        _ = [e async for e in loop.run("test")]
        assert invoked == ["yes"]


# ---------------------------------------------------------------------------
# hook_middleware
# ---------------------------------------------------------------------------


class TestHookMiddleware:
    @pytest.mark.asyncio
    async def test_pre_and_post_hooks_fire_around_dispatch(self) -> None:
        order: list[str] = []

        def t(**_: Any) -> str:
            order.append("dispatch")
            return "ok"

        class _Hooks:
            def run(self, name: str, *_args: Any) -> None:
                order.append(f"hook:{name}")

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "t"}]),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(
            backend,
            _registry({"t": t}),
            dispatch_middleware=[hook_middleware(_Hooks())],
        )
        _ = [e async for e in loop.run("test")]
        assert order == ["hook:pre_tool_use", "dispatch", "hook:post_tool_use"]

    @pytest.mark.asyncio
    async def test_hook_exception_is_swallowed(self) -> None:
        invoked: list[str] = []

        def t(**_: Any) -> str:
            invoked.append("yes")
            return "ok"

        class _BadHooks:
            def run(self, _name: str, *_args: Any) -> None:
                raise RuntimeError("boom")

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "t"}]),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(
            backend,
            _registry({"t": t}),
            dispatch_middleware=[hook_middleware(_BadHooks())],
        )
        _ = [e async for e in loop.run("test")]
        # Dispatch still ran despite hook raising.
        assert invoked == ["yes"]


# ---------------------------------------------------------------------------
# tool_output_level
# ---------------------------------------------------------------------------


class TestToolOutputLevel:
    @pytest.mark.asyncio
    async def test_silent_replaces_content_with_empty(self) -> None:
        def t(**_: Any) -> str:
            return "verbose-output-text-here"

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "t"}]),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(
            backend,
            _registry({"t": t}),
            dispatch_middleware=[tool_output_level({"t": "silent"})],
        )
        _ = [e async for e in loop.run("test")]
        last_user_msg = backend.received_messages[1][-1]
        result_block = next(b for b in last_user_msg.content if b.kind == "tool_result")
        # Silent → empty text in the tool_result.
        assert result_block.text == ""

    @pytest.mark.asyncio
    async def test_default_level_passes_output_through(self) -> None:
        def t(**_: Any) -> str:
            return "the actual result"

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "t"}]),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(
            backend,
            _registry({"t": t}),
            dispatch_middleware=[tool_output_level({}, default="standard")],
        )
        _ = [e async for e in loop.run("test")]
        last_user_msg = backend.received_messages[1][-1]
        result_block = next(b for b in last_user_msg.content if b.kind == "tool_result")
        assert "the actual result" in result_block.text


# ---------------------------------------------------------------------------
# Composition — multiple middleware layered together
# ---------------------------------------------------------------------------


class TestMiddlewareComposition:
    @pytest.mark.asyncio
    async def test_outer_gate_runs_before_inner_hooks(self) -> None:
        """capability_gate (outer) denies BEFORE hook_middleware (inner) fires."""
        order: list[str] = []

        def t(**_: Any) -> str:
            order.append("dispatch")
            return "ok"

        class _Token:
            def allows(self, _name: str) -> bool:
                return False

        class _Hooks:
            def run(self, name: str, *_args: Any) -> None:
                order.append(f"hook:{name}")

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "t"}]),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(
            backend,
            _registry({"t": t}),
            dispatch_middleware=[
                capability_gate(_Token()),  # outer
                hook_middleware(_Hooks()),  # inner
            ],
        )
        _ = [e async for e in loop.run("test")]
        # Gate denied, so neither hook nor dispatch ran.
        assert order == []

    @pytest.mark.asyncio
    async def test_full_stack_allowlist_then_confirm_then_hooks(self) -> None:
        order: list[str] = []

        def t(**_: Any) -> str:
            order.append("dispatch")
            return "ok"

        class _Hooks:
            def run(self, name: str, *_args: Any) -> None:
                order.append(f"hook:{name}")

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "t"}]),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(
            backend,
            _registry({"t": t}),
            dispatch_middleware=[
                tool_allowlist(["t"]),  # outer-most: allow
                tool_confirmation(lambda _n: True),  # mid
                hook_middleware(_Hooks()),  # inner-most
            ],
        )
        _ = [e async for e in loop.run("test")]
        assert order == ["hook:pre_tool_use", "dispatch", "hook:post_tool_use"]
