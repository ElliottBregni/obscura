"""StreamingToolExecutor decoupled-callback contract.

After the OOP refactor, the executor no longer holds a reference to
``AgentLoop``. It receives a ``tool_lookup`` callable + an
``execute_tool`` coroutine, and exposes its previously-protected fields
(``seen_calls``, ``order``, ``abort_event``, ``in_flight``) as part of
its public API.
"""

from __future__ import annotations

import asyncio

import pytest

from obscura.core.agent_loop import StreamingToolExecutor
from obscura.core.types import (
    ToolCallInfo,
    ToolErrorType,
    ToolExecutionError,
    ToolResultEnvelope,
    ToolSpec,
)


def _tc(call_id: str, name: str = "fake") -> ToolCallInfo:
    return ToolCallInfo(tool_use_id=call_id, name=name, input={})


def _ok_result(tc: ToolCallInfo) -> ToolResultEnvelope:
    return ToolResultEnvelope(
        call_id=tc.tool_use_id,
        tool=tc.name,
        status="ok",
        result="done",
        tool_use_id=tc.tool_use_id,
        raw=tc.raw,
    )


def _err_result(tc: ToolCallInfo, msg: str) -> ToolResultEnvelope:
    return ToolResultEnvelope(
        call_id=tc.tool_use_id,
        tool=tc.name,
        status="error",
        error=ToolExecutionError(
            type=ToolErrorType.UNKNOWN,
            message=msg,
            safe_to_retry=False,
        ),
        tool_use_id=tc.tool_use_id,
        raw=tc.raw,
    )


def _spec(name: str, *, side_effects: str = "none") -> ToolSpec:
    """Build a minimal ToolSpec; side_effects controls concurrency-safety."""
    return ToolSpec(
        name=name,
        description="",
        parameters={},
        handler=lambda: None,
        side_effects=side_effects,
    )


@pytest.mark.asyncio
async def test_executor_runs_via_callbacks_without_agent_loop() -> None:
    """The executor uses the injected callables — no AgentLoop reference."""
    safe_spec = _spec("safe")
    looked_up: list[str] = []
    executed: list[str] = []

    def lookup(name: str) -> ToolSpec | None:
        looked_up.append(name)
        return safe_spec

    async def execute(tc: ToolCallInfo, seen: dict[str, ToolResultEnvelope]) -> ToolResultEnvelope:
        executed.append(tc.tool_use_id)
        return _ok_result(tc)

    executor = StreamingToolExecutor(tool_lookup=lookup, execute_tool=execute)
    tc = _tc("call-1", "safe")
    executor.add_tool(tc)

    results = await executor.wait_for_all()

    assert [r.call_id for r in results] == ["call-1"]
    assert results[0].status == "ok"
    assert "call-1" in executed
    assert "safe" in looked_up


@pytest.mark.asyncio
async def test_results_emerge_in_submission_order() -> None:
    """Even when handlers complete out of order, results follow submission order."""
    safe_spec = _spec("safe")

    completion_order: list[str] = []

    async def execute(tc: ToolCallInfo, seen: dict[str, ToolResultEnvelope]) -> ToolResultEnvelope:
        # Make later submissions finish first
        delay = 0.05 if tc.tool_use_id == "first" else 0.0
        await asyncio.sleep(delay)
        completion_order.append(tc.tool_use_id)
        return _ok_result(tc)

    executor = StreamingToolExecutor(
        tool_lookup=lambda _: safe_spec,
        execute_tool=execute,
    )
    executor.add_tool(_tc("first", "safe"))
    executor.add_tool(_tc("second", "safe"))

    results = await executor.wait_for_all()

    # second finishes first internally...
    assert completion_order == ["second", "first"]
    # ...but results come back in submission order
    assert [r.call_id for r in results] == ["first", "second"]


@pytest.mark.asyncio
async def test_unsafe_tool_aborts_siblings_via_abort_event() -> None:
    """A non-concurrency-safe tool that errors signals sibling abort."""
    unsafe_spec = _spec("unsafe", side_effects="write")

    async def execute(tc: ToolCallInfo, seen: dict[str, ToolResultEnvelope]) -> ToolResultEnvelope:
        return _err_result(tc, "boom")

    executor = StreamingToolExecutor(
        tool_lookup=lambda _: unsafe_spec,
        execute_tool=execute,
    )
    executor.add_tool(_tc("a", "unsafe"))
    await executor.wait_for_all()

    # Public attribute (was _abort): exposed as abort_event after the rename
    assert executor.abort_event.is_set()


@pytest.mark.asyncio
async def test_seen_calls_dedup_cache_is_shared_with_caller() -> None:
    """The owner can hand in its dedup cache by assigning to ``seen_calls``."""
    safe_spec = _spec("safe")
    received: list[dict[str, ToolResultEnvelope]] = []

    async def execute(tc: ToolCallInfo, seen: dict[str, ToolResultEnvelope]) -> ToolResultEnvelope:
        received.append(seen)
        return _ok_result(tc)

    executor = StreamingToolExecutor(
        tool_lookup=lambda _: safe_spec,
        execute_tool=execute,
    )
    shared: dict[str, ToolResultEnvelope] = {}
    executor.seen_calls = shared
    executor.add_tool(_tc("c", "safe"))
    await executor.wait_for_all()

    assert received[0] is shared


@pytest.mark.asyncio
async def test_order_attribute_tracks_submission() -> None:
    """``executor.order`` (was ``_order``) is the canonical submission queue."""
    safe_spec = _spec("safe")

    async def execute(tc: ToolCallInfo, _: dict[str, ToolResultEnvelope]) -> ToolResultEnvelope:
        return _ok_result(tc)

    executor = StreamingToolExecutor(
        tool_lookup=lambda _: safe_spec,
        execute_tool=execute,
    )
    executor.add_tool(_tc("a", "safe"))
    executor.add_tool(_tc("b", "safe"))
    executor.add_tool(_tc("c", "safe"))

    assert executor.order == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_close_rejects_further_adds() -> None:
    """After ``close()``, ``add_tool`` is a no-op (executor is winding down)."""
    safe_spec = _spec("safe")

    async def execute(tc: ToolCallInfo, _: dict[str, ToolResultEnvelope]) -> ToolResultEnvelope:
        return _ok_result(tc)

    executor = StreamingToolExecutor(
        tool_lookup=lambda _: safe_spec,
        execute_tool=execute,
    )
    executor.close()
    executor.add_tool(_tc("late", "safe"))

    assert executor.order == []
    assert not executor.has_tools
