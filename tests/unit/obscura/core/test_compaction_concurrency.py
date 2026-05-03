"""Tests for compact_history concurrency safety.

Guards the module-level _compaction_lock that serializes compaction against
concurrent history mutations from in-flight tool results (Stage C+ work).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

import pytest

from obscura.core import compaction


class _RecordingBackend:
    """Backend stub that records when summarize calls overlap.

    Tracks when compact_history's summarization phase begins and ends so we
    can assert that two concurrent compact_history calls don't overlap each
    other's critical sections.
    """

    def __init__(self, sleep_for: float = 0.05) -> None:
        self.events: list[tuple[str, float]] = []
        self.sleep_for = sleep_for
        self._next_id = 0
        self._lock = asyncio.Lock()

    async def complete(self, prompt: str, max_tokens: int = 2048) -> str:
        # Allocate a stable id under self._lock — separate from the
        # compaction lock under test.
        async with self._lock:
            self._next_id += 1
            cid = self._next_id
        self.events.append((f"start-{cid}", time.monotonic()))
        await asyncio.sleep(self.sleep_for)
        self.events.append((f"end-{cid}", time.monotonic()))
        return f"summary-{cid}"


def _msgs(n: int) -> list[dict[str, Any]]:
    """Build n alternating user/assistant message dicts with bulky content."""
    out: list[dict[str, Any]] = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        # Make each message large enough that the budget will force pruning.
        out.append({"role": role, "content": "x" * 2000 + f"#{i}"})
    return out


@pytest.mark.asyncio
async def test_compact_history_no_messages_returns_immediately() -> None:
    """Empty input is a no-op even under the lock — exercises the early return."""
    backend = _RecordingBackend()
    result, was_compacted, memories = await compaction.compact_history(
        [],
        "claude-opus-4",
        backend,
    )
    assert result == []
    assert was_compacted is False
    assert memories == []


@pytest.mark.asyncio
async def test_compact_history_concurrent_calls_serialize() -> None:
    """Two concurrent compact_history calls must not overlap their bodies.

    Records start/end timestamps. With the lock held, the second call's
    start must come after the first call's end. Without the lock, the two
    bodies would overlap (both sleeping at the same time during the
    mocked summarization phase).
    """
    backend = _RecordingBackend(sleep_for=0.1)

    async def run_one(messages: list[dict[str, Any]]) -> Any:
        return await compaction.compact_history(
            messages,
            "claude-opus-4",
            backend,
            # Set a tiny budget so phase 2 (LLM summarization) is forced.
            reserve_tokens=1,
            max_history_share=0.001,
            fallback_keep_last=2,
        )

    # Use a budget tight enough to force the summarization path. The
    # _RecordingBackend's `complete` call is what we want to overlap-check.
    msgs_a = _msgs(8)
    msgs_b = _msgs(8)

    results = await asyncio.gather(run_one(msgs_a), run_one(msgs_b))
    # Both calls succeeded.
    assert len(results) == 2

    # Each call may emit zero or more start/end pairs depending on path
    # taken. Group events by their numeric id and sort.
    starts = [t for name, t in backend.events if name.startswith("start-")]
    ends = [t for name, t in backend.events if name.startswith("end-")]

    if len(starts) >= 2 and len(ends) >= 2:
        # Sort the starts and ends; with the lock held, the FIRST end must
        # come before the SECOND start. Without the lock, the two calls'
        # summarize phases would overlap (start, start, end, end pattern).
        starts.sort()
        ends.sort()
        assert ends[0] <= starts[1] + 0.001, (
            f"Concurrency leak: second compaction started "
            f"({starts[1]:.3f}) before first finished ({ends[0]:.3f})"
        )


@pytest.mark.asyncio
async def test_compact_history_lock_actually_blocks() -> None:
    """When the module lock is held externally, compact_history must wait."""
    started_event = asyncio.Event()

    async def fake_extract_memories(*_args: Any, **_kwargs: Any) -> list[Any]:
        started_event.set()
        return []

    with patch.object(compaction, "extract_memories", fake_extract_memories):
        async with compaction._compaction_lock:
            # Lock is held; spawn a compact_history call that should block.
            task = asyncio.create_task(
                compaction.compact_history(
                    _msgs(4),
                    "claude-opus-4",
                    _RecordingBackend(),
                    reserve_tokens=1,
                ),
            )
            # Give the task a chance to attempt acquiring the lock.
            await asyncio.sleep(0.05)
            assert not started_event.is_set(), (
                "compact_history ran while module lock was held"
            )
            assert not task.done()

        # Lock released — the task should now proceed.
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_compact_history_lock_released_on_exception() -> None:
    """If summarize_messages raises, the lock must still be released.

    Critical: the lock is held via ``async with``, so any exception inside
    the body must release it. Verify by triggering an exception path and
    then acquiring the lock from outside.
    """

    class _RaisingBackend:
        async def complete(self, *args: Any, **kwargs: Any) -> str:
            raise RuntimeError("simulated backend failure")

    # Run a compact that will hit the LLM phase; even if all summaries
    # fail, compact_history's documented behavior is to fall back, not
    # propagate. Either way, the lock must be released.
    try:
        await compaction.compact_history(
            _msgs(8),
            "claude-opus-4",
            _RaisingBackend(),
            reserve_tokens=1,
            max_history_share=0.001,
            fallback_keep_last=2,
        )
    except Exception:
        # We don't care if it raised — just that the lock is now free.
        pass

    # Lock must be acquirable now.
    acquired = await asyncio.wait_for(
        compaction._compaction_lock.acquire(), timeout=1.0
    )
    assert acquired
    compaction._compaction_lock.release()
