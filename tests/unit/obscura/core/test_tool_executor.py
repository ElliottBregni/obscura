"""Tests for obscura.core.tool_executor — concurrent tool execution engine."""

from __future__ import annotations

import asyncio
import time

from obscura.core.tool_executor import (
    ConcurrentToolExecutor,
    ToolBatch,
    partition_tool_calls,
)
from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    ToolCallInfo,
    ToolErrorType,
    ToolExecutionError,
    ToolResultEnvelope,
    ToolSpec,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(name: str, side_effects: str = "none") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"Test tool {name}",
        parameters={},
        handler=lambda: None,
        side_effects=side_effects,
    )


def _make_tc(name: str, tool_use_id: str | None = None) -> ToolCallInfo:
    return ToolCallInfo(
        tool_use_id=tool_use_id or f"id-{name}",
        name=name,
        input={},
    )


def _ok_result(tc: ToolCallInfo, value: str = "ok") -> ToolResultEnvelope:
    return ToolResultEnvelope(
        call_id=tc.tool_use_id,
        tool=tc.name,
        status="ok",
        result=value,
        tool_use_id=tc.tool_use_id,
    )


# ---------------------------------------------------------------------------
# partition_tool_calls
# ---------------------------------------------------------------------------


class TestPartition:
    def test_read_only_concurrent(self) -> None:
        reg = ToolRegistry()
        reg.register(_make_spec("read_file", side_effects="read"))
        reg.register(_make_spec("grep", side_effects="read"))

        calls = [_make_tc("read_file"), _make_tc("grep")]
        batch = partition_tool_calls(calls, reg)

        assert len(batch.concurrent) == 2
        assert len(batch.sequential) == 0

    def test_write_sequential(self) -> None:
        reg = ToolRegistry()
        reg.register(_make_spec("write_file", side_effects="write"))

        calls = [_make_tc("write_file")]
        batch = partition_tool_calls(calls, reg)

        assert len(batch.concurrent) == 0
        assert len(batch.sequential) == 1

    def test_mixed_partition(self) -> None:
        reg = ToolRegistry()
        reg.register(_make_spec("read_file", side_effects="read"))
        reg.register(_make_spec("write_file", side_effects="write"))
        reg.register(_make_spec("pure_fn", side_effects="none"))

        calls = [_make_tc("read_file"), _make_tc("write_file"), _make_tc("pure_fn")]
        batch = partition_tool_calls(calls, reg)

        assert len(batch.concurrent) == 2
        assert len(batch.sequential) == 1
        assert batch.sequential[0].name == "write_file"

    def test_unknown_tool_sequential(self) -> None:
        reg = ToolRegistry()
        calls = [_make_tc("unknown_tool")]
        batch = partition_tool_calls(calls, reg)

        assert len(batch.concurrent) == 0
        assert len(batch.sequential) == 1

    def test_empty_calls(self) -> None:
        reg = ToolRegistry()
        batch = partition_tool_calls([], reg)
        assert batch == ToolBatch()


# ---------------------------------------------------------------------------
# ConcurrentToolExecutor
# ---------------------------------------------------------------------------


class TestConcurrentToolExecutor:
    async def test_concurrent_execution_is_parallel(self) -> None:
        """Read-only tools should run concurrently (total time ~ max, not sum)."""
        reg = ToolRegistry()
        reg.register(_make_spec("slow_read", side_effects="read"))

        async def _slow_execute(tc: ToolCallInfo) -> ToolResultEnvelope:
            await asyncio.sleep(0.1)
            return _ok_result(tc, f"result-{tc.name}")

        calls = [_make_tc("slow_read", f"id-{i}") for i in range(3)]
        executor = ConcurrentToolExecutor()

        start = time.monotonic()
        results = await executor.execute_batch(calls, _slow_execute, reg)
        elapsed = time.monotonic() - start

        assert len(results) == 3
        assert all(r.status == "ok" for r in results)
        # 3 tasks * 0.1s should take ~0.1s concurrent, not 0.3s sequential
        assert elapsed < 0.25

    async def test_sequential_mutations(self) -> None:
        """Write tools should run sequentially."""
        reg = ToolRegistry()
        reg.register(_make_spec("write_op", side_effects="write"))

        order: list[str] = []

        async def _ordered_execute(tc: ToolCallInfo) -> ToolResultEnvelope:
            order.append(tc.tool_use_id)
            await asyncio.sleep(0.01)
            return _ok_result(tc)

        calls = [_make_tc("write_op", f"w-{i}") for i in range(3)]
        executor = ConcurrentToolExecutor()
        results = await executor.execute_batch(calls, _ordered_execute, reg)

        assert len(results) == 3
        assert order == ["w-0", "w-1", "w-2"]

    async def test_concurrent_then_sequential(self) -> None:
        """Mixed batch: reads run first concurrently, then writes sequentially."""
        reg = ToolRegistry()
        reg.register(_make_spec("read_op", side_effects="read"))
        reg.register(_make_spec("write_op", side_effects="write"))

        order: list[str] = []

        async def _tracking_execute(tc: ToolCallInfo) -> ToolResultEnvelope:
            order.append(tc.tool_use_id)
            return _ok_result(tc)

        calls = [
            _make_tc("read_op", "r-0"),
            _make_tc("write_op", "w-0"),
            _make_tc("read_op", "r-1"),
        ]
        executor = ConcurrentToolExecutor()
        results = await executor.execute_batch(calls, _tracking_execute, reg)

        assert len(results) == 3
        # Reads should execute before writes
        read_indices = [order.index("r-0"), order.index("r-1")]
        write_index = order.index("w-0")
        assert all(ri < write_index for ri in read_indices)

    async def test_results_in_original_order(self) -> None:
        """Results should be returned in the same order as tool_calls."""
        reg = ToolRegistry()
        reg.register(_make_spec("read_op", side_effects="read"))
        reg.register(_make_spec("write_op", side_effects="write"))

        async def _execute(tc: ToolCallInfo) -> ToolResultEnvelope:
            return _ok_result(tc, f"val-{tc.tool_use_id}")

        calls = [
            _make_tc("read_op", "a"),
            _make_tc("write_op", "b"),
            _make_tc("read_op", "c"),
        ]
        executor = ConcurrentToolExecutor()
        results = await executor.execute_batch(calls, _execute, reg)

        assert [r.call_id for r in results] == ["a", "b", "c"]

    async def test_sibling_abort_on_error(self) -> None:
        """If a concurrent tool errors, siblings should be cancelled."""
        reg = ToolRegistry()
        reg.register(_make_spec("fast_fail", side_effects="read"))
        reg.register(_make_spec("slow_read", side_effects="read"))

        async def _mixed_execute(tc: ToolCallInfo) -> ToolResultEnvelope:
            if tc.name == "fast_fail":
                return ToolResultEnvelope(
                    call_id=tc.tool_use_id,
                    tool=tc.name,
                    status="error",
                    error=ToolExecutionError(
                        type=ToolErrorType.UNKNOWN,
                        message="intentional failure",
                    ),
                    tool_use_id=tc.tool_use_id,
                )
            await asyncio.sleep(10)  # Should be cancelled
            return _ok_result(tc)

        calls = [
            _make_tc("fast_fail", "fail"),
            _make_tc("slow_read", "slow"),
        ]
        executor = ConcurrentToolExecutor()

        start = time.monotonic()
        results = await executor.execute_batch(calls, _mixed_execute, reg)
        elapsed = time.monotonic() - start

        assert len(results) == 2
        # The slow task should have been cancelled quickly
        assert elapsed < 2.0
        # At least one result should be an error
        error_results = [r for r in results if r.status == "error"]
        assert len(error_results) >= 1

    async def test_pre_started_tasks_reused(self) -> None:
        """Pre-started streaming tasks should be reused, not re-executed."""
        reg = ToolRegistry()
        reg.register(_make_spec("read_op", side_effects="read"))

        call_count = 0

        async def _counting_execute(tc: ToolCallInfo) -> ToolResultEnvelope:
            nonlocal call_count
            call_count += 1
            return _ok_result(tc, "from-executor")

        tc = _make_tc("read_op", "pre-1")
        executor = ConcurrentToolExecutor()

        # Pre-start the task
        pre_task = executor.start_streaming(tc, _counting_execute)
        pre_started = {"pre-1": pre_task}

        # Execute batch — should reuse pre-started task
        results = await executor.execute_batch(
            [tc],
            _counting_execute,
            reg,
            pre_started=pre_started,
        )

        assert len(results) == 1
        assert results[0].status == "ok"
        # Should only have been called once (by start_streaming), not twice
        assert call_count == 1

    async def test_semaphore_limits_concurrency(self) -> None:
        """Semaphore should limit max concurrent tasks."""
        reg = ToolRegistry()
        reg.register(_make_spec("read_op", side_effects="read"))

        max_concurrent = 0
        current_concurrent = 0

        async def _tracking_execute(tc: ToolCallInfo) -> ToolResultEnvelope:
            nonlocal max_concurrent, current_concurrent
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
            await asyncio.sleep(0.05)
            current_concurrent -= 1
            return _ok_result(tc)

        executor = ConcurrentToolExecutor(max_concurrent=3)
        calls = [_make_tc("read_op", f"id-{i}") for i in range(9)]

        await executor.execute_batch(calls, _tracking_execute, reg)
        assert max_concurrent <= 3

    async def test_empty_batch(self) -> None:
        reg = ToolRegistry()
        executor = ConcurrentToolExecutor()
        results = await executor.execute_batch([], lambda tc: None, reg)  # type: ignore[arg-type]
        assert results == []

    async def test_start_streaming(self) -> None:
        """start_streaming should return an awaitable Task."""

        async def _execute(tc: ToolCallInfo) -> ToolResultEnvelope:
            return _ok_result(tc, "streamed")

        tc = _make_tc("read_op", "stream-1")
        executor = ConcurrentToolExecutor()

        task = executor.start_streaming(tc, _execute)
        assert isinstance(task, asyncio.Task)
        result = await task
        assert result.status == "ok"
        assert result.result == "streamed"
