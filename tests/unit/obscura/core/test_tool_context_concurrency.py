"""Tests for ToolContext.append_history concurrency safety.

These tests guard against regression once the agent loop runs tools in
parallel under a DAG executor. Without ToolContext.history_lock, two tasks
that both append to the same history list can race on Python's list
internals (though CPython's GIL serializes the actual append op, the lock
also covers compound check-then-mutate sequences in real callsites).
"""

from __future__ import annotations

import asyncio

import pytest

from obscura.core.tool_context import ToolContext


@pytest.mark.asyncio
async def test_append_history_no_history_is_noop() -> None:
    """append_history is a no-op when ctx.history is None."""
    ctx = ToolContext(history=None)
    await ctx.append_history("anything")
    # No exception, history still None.
    assert ctx.history is None


@pytest.mark.asyncio
async def test_append_history_serial_appends() -> None:
    """Sequential appends produce the expected list."""
    history: list[str] = []
    ctx = ToolContext(history=history)
    for i in range(5):
        await ctx.append_history(f"msg-{i}")
    assert history == ["msg-0", "msg-1", "msg-2", "msg-3", "msg-4"]


@pytest.mark.asyncio
async def test_append_history_concurrent_no_loss() -> None:
    """Two concurrent appenders each adding 100 unique messages all land.

    With the lock, all 200 messages must be present in the final list.
    The order between the two streams is unspecified, but no message can
    be lost. This is the core property that protects against the future
    DAG-executor scenario.
    """
    history: list[str] = []
    ctx = ToolContext(history=history)

    async def appender(prefix: str) -> None:
        for i in range(100):
            await ctx.append_history(f"{prefix}-{i}")
            # Yield occasionally to encourage interleaving.
            if i % 7 == 0:
                await asyncio.sleep(0)

    await asyncio.gather(appender("A"), appender("B"))

    assert len(history) == 200
    a_msgs = {m for m in history if m.startswith("A-")}
    b_msgs = {m for m in history if m.startswith("B-")}
    assert a_msgs == {f"A-{i}" for i in range(100)}
    assert b_msgs == {f"B-{i}" for i in range(100)}


@pytest.mark.asyncio
async def test_append_history_lock_actually_serializes() -> None:
    """The lock must serialize critical sections, not just compute order.

    Acquire the lock externally and verify that an append blocks until
    released. This ensures the lock isn't being circumvented by a fast path.
    """
    history: list[str] = []
    ctx = ToolContext(history=history)

    # Hold the lock externally; the append should block on it.
    async with ctx.history_lock:
        async def try_append() -> None:
            await ctx.append_history("blocked")

        task = asyncio.create_task(try_append())
        # Give it a chance to attempt acquisition.
        await asyncio.sleep(0.05)
        # Lock is held by us, so the task hasn't appended yet.
        assert history == []
        assert not task.done()

    # Lock released — task should now complete.
    await task
    assert history == ["blocked"]


@pytest.mark.asyncio
async def test_history_lock_per_context_instance() -> None:
    """Each ToolContext gets its own lock — no cross-binding contamination."""
    ctx_a = ToolContext(history=[])
    ctx_b = ToolContext(history=[])
    assert ctx_a.history_lock is not ctx_b.history_lock
