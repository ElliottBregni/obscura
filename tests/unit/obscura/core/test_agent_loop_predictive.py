"""Tests for the v2 predictive cache: cache primitives + middleware + observer.

The cache stores asyncio Tasks of speculative tool dispatches. The middleware
checks the cache on each dispatch; on hit, returns the speculative result
without invoking the inner chain. The observer feeds text deltas to the
predictor and starts speculative dispatches for read-only matches.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from obscura.core.agent_loop_predictive import (
    V2PredictiveCache,
    make_predictive_observer,
    predictive_cache_middleware,
)
from obscura.core.dag import DAGNode
from obscura.core.tool_context import ToolContext
from obscura.core.tools import ToolRegistry
from obscura.core.types import ContentBlock, ToolSpec
from obscura.runtime.predictive_tools import ToolPredictor


# ---------------------------------------------------------------------------
# V2PredictiveCache primitives
# ---------------------------------------------------------------------------


class TestV2PredictiveCache:
    @pytest.mark.asyncio
    async def test_put_and_pop_returns_task_result(self) -> None:
        cache = V2PredictiveCache()

        async def mk_task() -> list[ContentBlock]:
            return [ContentBlock(kind="text", text="ok")]

        task = asyncio.create_task(mk_task())
        cache.put("read_text_file", {"path": "/tmp/x"}, task)
        result = await cache.pop_and_await("read_text_file", {"path": "/tmp/x"})
        assert result is not None
        assert result[0].text == "ok"
        assert cache.stats["hits"] == 1

    @pytest.mark.asyncio
    async def test_pop_on_miss_returns_none(self) -> None:
        cache = V2PredictiveCache()
        result = await cache.pop_and_await("nope", {})
        assert result is None
        assert cache.stats["misses"] == 1

    @pytest.mark.asyncio
    async def test_speculation_failure_returns_none_not_raise(self) -> None:
        cache = V2PredictiveCache()

        async def boom() -> list[ContentBlock]:
            raise RuntimeError("speculation failed")

        task = asyncio.create_task(boom())
        cache.put("t", {}, task)
        result = await cache.pop_and_await("t", {})
        assert result is None  # Falls through; caller re-executes.

    @pytest.mark.asyncio
    async def test_evicts_oldest_when_full(self) -> None:
        cache = V2PredictiveCache(max_entries=2)

        async def mk_task(text: str) -> list[ContentBlock]:
            await asyncio.sleep(1.0)
            return [ContentBlock(kind="text", text=text)]

        t1 = asyncio.create_task(mk_task("first"))
        t2 = asyncio.create_task(mk_task("second"))
        t3 = asyncio.create_task(mk_task("third"))
        cache.put("a", {}, t1)
        cache.put("b", {}, t2)
        cache.put("c", {}, t3)
        # First entry should have had its task cancelled. Yield to let
        # the cancellation propagate (asyncio cancellation is delivered
        # at await boundaries).
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert t1.cancelled() or t1.done()
        assert cache.has("b", {})
        assert cache.has("c", {})
        # Cleanup — cancel pending tasks before exit.
        cache.clear()
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_clear_cancels_pending_tasks(self) -> None:
        cache = V2PredictiveCache()

        async def slow() -> list[ContentBlock]:
            await asyncio.sleep(10.0)
            return []

        task = asyncio.create_task(slow())
        cache.put("t", {}, task)
        cache.clear()
        # Give cancellation a chance to propagate.
        await asyncio.sleep(0)
        assert task.cancelled()


# ---------------------------------------------------------------------------
# predictive_cache_middleware
# ---------------------------------------------------------------------------


class TestPredictiveCacheMiddleware:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_inner(self) -> None:
        cache = V2PredictiveCache()
        invocations: list[str] = []

        async def mk_task() -> list[ContentBlock]:
            return [ContentBlock(kind="text", text="speculated")]

        task = asyncio.create_task(mk_task())
        cache.put("read_text_file", {"path": "/tmp/x"}, task)

        async def inner(_node: Any, _resolved: Any) -> list[ContentBlock]:
            invocations.append("inner")
            return [ContentBlock(kind="text", text="real")]

        wrapped = predictive_cache_middleware(cache)(inner)
        node = DAGNode(
            id="tu_1",
            tool_name="read_text_file",
            tool_input={"path": "/tmp/x"},
            depends_on=(),
            submission_index=0,
            tool_use_id="tu_1",
        )
        result = await wrapped(node, {})

        assert result[0].text == "speculated"
        assert invocations == []  # inner was bypassed

    @pytest.mark.asyncio
    async def test_cache_miss_falls_through_to_inner(self) -> None:
        cache = V2PredictiveCache()
        invocations: list[str] = []

        async def inner(_node: Any, _resolved: Any) -> list[ContentBlock]:
            invocations.append("inner")
            return [ContentBlock(kind="text", text="real")]

        wrapped = predictive_cache_middleware(cache)(inner)
        node = DAGNode(
            id="tu_1",
            tool_name="t",
            tool_input={},
            depends_on=(),
            submission_index=0,
            tool_use_id="tu_1",
        )
        result = await wrapped(node, {})

        assert result[0].text == "real"
        assert invocations == ["inner"]


# ---------------------------------------------------------------------------
# make_predictive_observer
# ---------------------------------------------------------------------------


class TestPredictiveObserver:
    @pytest.mark.asyncio
    async def test_observer_speculates_for_readonly_pattern(self) -> None:
        """When the model says "Let me read foo.py", the observer should
        speculatively dispatch read_text_file({"path": "foo.py"}) and put
        a task in the cache."""
        invocations: list[str] = []

        def read_text_file(path: str = "") -> str:
            invocations.append(path)
            return f"contents of {path}"

        reg = ToolRegistry()
        reg.register(
            ToolSpec(
                name="read_text_file",
                description="reads",
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                },
                handler=read_text_file,
                side_effects="none",  # essential for speculation
            )
        )

        cache = V2PredictiveCache()
        predictor = ToolPredictor(tool_registry={s.name: s for s in reg.all()})
        ctx = ToolContext(registry=reg)
        observer = make_predictive_observer(
            predictor=predictor, cache=cache, registry=reg, tool_ctx=ctx
        )

        # Feed enough text to trigger the predictor (the predictor needs
        # some buffer to start; feed in chunks).
        await observer("Let me read ")
        await observer("foo.py to see what's there.")

        # Allow the speculative task to complete.
        await asyncio.sleep(0.05)

        # Cache should have a hit when we look up the predicted call.
        result = await cache.pop_and_await("read_text_file", {"path": "foo.py"})
        assert result is not None
        # The handler was actually invoked.
        assert invocations == ["foo.py"]

    @pytest.mark.asyncio
    async def test_observer_skips_side_effecting_tools(self) -> None:
        """Tools with side_effects != "none" must NEVER be speculated, even
        if a pattern matches."""
        invocations: list[str] = []

        def write_file(path: str = "") -> str:
            invocations.append(path)
            return "wrote"

        reg = ToolRegistry()
        reg.register(
            ToolSpec(
                name="write_file",
                description="writes",
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                },
                handler=write_file,
                side_effects="writes:fs",
            )
        )

        cache = V2PredictiveCache()
        predictor = ToolPredictor(tool_registry={s.name: s for s in reg.all()})
        ctx = ToolContext(registry=reg)
        observer = make_predictive_observer(
            predictor=predictor, cache=cache, registry=reg, tool_ctx=ctx
        )

        await observer("I'll write file foo.py with some content.")
        await asyncio.sleep(0.05)

        # Even if a pattern fired, write_file should NOT be in the cache.
        # (Predictor only returns concurrency-safe tools, but verify.)
        assert invocations == []
