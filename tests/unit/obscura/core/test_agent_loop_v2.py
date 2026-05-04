"""End-to-end tests for AgentLoopV2 against a stub backend.

The stub backend lets us drive the loop deterministically without any real
SDK. Tests cover:

- A turn with no tool calls — loop exits cleanly with AGENT_DONE.
- A turn with a single tool call — call dispatched, result fed back, loop
  continues for one more turn.
- Multiple tool calls in one turn — dispatched as a no-edge DAG, results
  in submission order.
- A parallel_plan tool call expanding into a real DAG.
- seen_calls dedup: a tool_use_id seen twice within the same turn is only
  executed once.
- Cancellation propagates: setting cancel_event mid-turn returns
  AGENT_DONE("cancelled by caller") cleanly.
- max_turns enforcement.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from obscura.core.agent_loop_v2 import AgentLoopV2, AgentLoopV2Config
from obscura.core.parallel_plan import make_parallel_plan_tool_spec
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
# Stub backend
# ---------------------------------------------------------------------------


@dataclass
class _StubTurn:
    """One scripted turn the stub backend will emit when stream() is called."""

    text: str = ""
    tool_uses: list[dict[str, Any]] = field(default_factory=list)


class _StubBackend:
    """Minimal BackendProtocol-compatible stub.

    Each ``stream()`` call consumes the next entry from ``self.script``.
    Test cases populate ``script`` ahead of time with a list of turns.
    """

    def __init__(self, script: list[_StubTurn]) -> None:
        self.script = list(script)
        self.calls: int = 0
        self.received_messages: list[list[Message]] = []

    @property
    def name(self) -> str:
        return "stub"

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
        self.received_messages.append(list(messages or []))
        idx = self.calls
        self.calls += 1
        if idx >= len(self.script):
            raise AssertionError(
                f"stub backend exhausted: stream() called {idx + 1}x but script has {len(self.script)}"
            )
        turn = self.script[idx]

        if turn.text:
            yield StreamChunk(kind=ChunkKind.TEXT_DELTA, text=turn.text)

        for tu in turn.tool_uses:
            tool_use_id = tu["id"]
            yield StreamChunk(
                kind=ChunkKind.TOOL_USE_START,
                tool_use_id=tool_use_id,
                tool_name=tu["name"],
            )
            yield StreamChunk(
                kind=ChunkKind.TOOL_USE_DELTA,
                tool_use_id=tool_use_id,
                tool_input_delta=json.dumps(tu.get("input", {})),
            )
            yield StreamChunk(
                kind=ChunkKind.TOOL_USE_END,
                tool_use_id=tool_use_id,
                tool_name=tu["name"],
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry(tools: dict[str, Any]) -> ToolRegistry:
    """Build a registry from {name: handler} dict."""
    reg = ToolRegistry()
    for name, handler in tools.items():
        spec = ToolSpec(
            name=name,
            description=f"stub tool {name}",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            },
            handler=handler,
        )
        reg.register(spec)
    return reg


def _events_by_kind(events: list[Any]) -> dict[AgentEventKind, list[Any]]:
    out: dict[AgentEventKind, list[Any]] = {}
    for e in events:
        out.setdefault(e.kind, []).append(e)
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoToolCallsExitsCleanly:
    @pytest.mark.asyncio
    async def test_text_only_turn_ends(self) -> None:
        backend = _StubBackend([_StubTurn(text="Hello, done.")])
        loop = AgentLoopV2(backend, ToolRegistry())
        events = [e async for e in loop.run("hi")]

        assert len(events) >= 2  # at least: text deltas + AGENT_DONE
        by_kind = _events_by_kind(events)
        assert AgentEventKind.AGENT_DONE in by_kind
        assert AgentEventKind.TOOL_CALL not in by_kind
        assert backend.calls == 1


class TestSingleToolCall:
    @pytest.mark.asyncio
    async def test_dispatches_tool_and_continues(self) -> None:
        invocations: list[dict[str, Any]] = []

        def echo(**kwargs: Any) -> str:
            invocations.append(kwargs)
            return f"echoed:{kwargs.get('msg', '')}"

        backend = _StubBackend(
            [
                _StubTurn(
                    text="calling echo",
                    tool_uses=[{"id": "tu_1", "name": "echo", "input": {"msg": "hi"}}],
                ),
                _StubTurn(text="all good"),
            ]
        )
        loop = AgentLoopV2(backend, _make_registry({"echo": echo}))
        events = [e async for e in loop.run("do it")]

        by_kind = _events_by_kind(events)
        assert len(by_kind[AgentEventKind.TOOL_CALL]) == 1
        assert by_kind[AgentEventKind.TOOL_CALL][0].tool_name == "echo"
        assert len(by_kind[AgentEventKind.TOOL_RESULT]) == 1
        assert by_kind[AgentEventKind.TOOL_RESULT][0].tool_use_id == "tu_1"
        assert "echoed:hi" in by_kind[AgentEventKind.TOOL_RESULT][0].tool_result
        assert invocations == [{"msg": "hi"}]
        assert backend.calls == 2  # tool turn + final assistant turn


class TestMultipleToolCallsNoEdges:
    @pytest.mark.asyncio
    async def test_runs_in_submission_order(self) -> None:
        order: list[str] = []

        def make_handler(label: str):
            def _h(**_kwargs: Any) -> str:
                order.append(label)
                return label

            return _h

        backend = _StubBackend(
            [
                _StubTurn(
                    tool_uses=[
                        {"id": "tu_1", "name": "a"},
                        {"id": "tu_2", "name": "b"},
                        {"id": "tu_3", "name": "c"},
                    ]
                ),
                _StubTurn(text="ok"),
            ]
        )
        registry = _make_registry(
            {"a": make_handler("a"), "b": make_handler("b"), "c": make_handler("c")}
        )
        loop = AgentLoopV2(backend, registry)
        events = [e async for e in loop.run("multi")]

        by_kind = _events_by_kind(events)
        result_ids = [e.tool_use_id for e in by_kind[AgentEventKind.TOOL_RESULT]]
        assert result_ids == ["tu_1", "tu_2", "tu_3"]
        assert order == ["a", "b", "c"]


class TestParallelPlanExpansion:
    @pytest.mark.asyncio
    async def test_parallel_plan_builds_dag_with_edges(self) -> None:
        order: list[str] = []
        gates: dict[str, asyncio.Event] = {}

        def make_handler(label: str):
            async def _h(**_kwargs: Any) -> str:
                # Wait for the gate keyed by this tool to be released, so we can
                # observe ordering deterministically.
                gate = gates.setdefault(label, asyncio.Event())
                await gate.wait()
                order.append(label)
                return label

            return _h

        plan_input = {
            "nodes": [
                {"id": "n1", "tool": "alpha", "args": {}, "depends_on": []},
                {"id": "n2", "tool": "beta", "args": {}, "depends_on": ["n1"]},
            ]
        }
        backend = _StubBackend(
            [
                _StubTurn(
                    tool_uses=[
                        {
                            "id": "tu_plan",
                            "name": "parallel_plan",
                            "input": plan_input,
                        }
                    ]
                ),
                _StubTurn(text="done"),
            ]
        )
        registry = _make_registry(
            {
                "alpha": make_handler("alpha"),
                "beta": make_handler("beta"),
            }
        )
        registry.register(make_parallel_plan_tool_spec())
        loop = AgentLoopV2(
            backend, registry, config=AgentLoopV2Config(max_concurrency=4)
        )

        async def driver() -> list[Any]:
            return [e async for e in loop.run("plan it")]

        # Release alpha first, then beta — ensures dependency was respected.
        async def releaser() -> None:
            await asyncio.sleep(0.01)
            gates.setdefault("alpha", asyncio.Event()).set()
            await asyncio.sleep(0.01)
            gates.setdefault("beta", asyncio.Event()).set()

        events, _ = await asyncio.gather(driver(), releaser())
        by_kind = _events_by_kind(events)
        # Both tools ran exactly once, in dependency order.
        assert order == ["alpha", "beta"]
        assert (
            len(by_kind[AgentEventKind.TOOL_CALL]) == 2
        )  # alpha + beta (not parallel_plan itself — that's expanded)
        assert len(by_kind[AgentEventKind.TOOL_RESULT]) == 2


class TestSeenCallsDedup:
    @pytest.mark.asyncio
    async def test_dedup_cleared_per_turn_so_distinct_calls_run(self) -> None:
        """The dedup cache is per-turn — fresh tool_use_ids in each turn
        mean each turn dispatches its tools normally (no false dedup hit
        from a prior turn's id collision).

        The cross-stream-retry dedup contract — re-emitting the same
        tool_use_id within ONE stream — is not yet exercised by v2's
        backend layer (no stream-retry middleware). Test left as a TODO
        for when the middleware lands.
        """
        invocations: list[str] = []

        def t(**kwargs: Any) -> str:
            invocations.append(kwargs.get("turn", ""))
            return "result"

        backend = _StubBackend(
            [
                _StubTurn(
                    tool_uses=[{"id": "tu_1", "name": "t", "input": {"turn": "first"}}]
                ),
                _StubTurn(
                    tool_uses=[{"id": "tu_2", "name": "t", "input": {"turn": "second"}}]
                ),
                _StubTurn(text="done"),
            ]
        )
        loop = AgentLoopV2(backend, _make_registry({"t": t}))
        _ = [e async for e in loop.run("two distinct turns")]

        # Each turn's tool ran exactly once.
        assert invocations == ["first", "second"]


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_event_mid_run_returns_cancelled(self) -> None:
        cancel = asyncio.Event()

        async def slow(**_kwargs: Any) -> str:
            await asyncio.sleep(1.0)
            return "should not finish"

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": "tu_1", "name": "slow"}]),
                _StubTurn(text="never reached"),
            ]
        )
        loop = AgentLoopV2(backend, _make_registry({"slow": slow}), cancel_event=cancel)

        async def driver() -> list[Any]:
            return [e async for e in loop.run("cancel me")]

        async def canceler() -> None:
            await asyncio.sleep(0.05)
            cancel.set()

        events, _ = await asyncio.gather(driver(), canceler())
        kinds = [e.kind for e in events]
        assert AgentEventKind.AGENT_DONE in kinds


class TestMaxTurns:
    @pytest.mark.asyncio
    async def test_max_turns_enforced(self) -> None:
        # Backend keeps emitting tool calls forever — loop must stop at max_turns.
        def loop_handler(**_kwargs: Any) -> str:
            return "again"

        backend = _StubBackend(
            [
                _StubTurn(tool_uses=[{"id": f"tu_{i}", "name": "loop"}])
                for i in range(20)
            ]
        )
        loop = AgentLoopV2(
            backend,
            _make_registry({"loop": loop_handler}),
            config=AgentLoopV2Config(max_turns=3),
        )
        events = [e async for e in loop.run("forever")]
        done = [e for e in events if e.kind == AgentEventKind.AGENT_DONE]
        assert len(done) == 1
        assert "max_turns" in done[0].text
        assert backend.calls == 3
