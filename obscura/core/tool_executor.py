"""Concurrent tool execution engine.

Partitions tool call batches into concurrent-safe (read-only) and serial
(mutation) groups based on the ``side_effects`` field on ``ToolSpec``.
Read-only tools run concurrently under a semaphore (max 10).  If any
concurrent tool errors, sibling tasks are cancelled immediately.

Supports *streaming execution*: read-only tools can be kicked off during
the model's response stream via :meth:`start_streaming`, and the pre-started
tasks are reused in :meth:`execute_batch` so the results are ready sooner.

Usage::

    executor = ConcurrentToolExecutor()
    results = await executor.execute_batch(
        tool_calls, execute_fn=my_handler, registry=tool_registry,
    )
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from obscura.core.types import (
    ToolCallInfo,
    ToolErrorType,
    ToolExecutionError,
    ToolResultEnvelope,
)

if TYPE_CHECKING:
    from obscura.core.tools import ToolRegistry

logger = logging.getLogger(__name__)

# Maximum number of read-only tools that execute concurrently.
_MAX_CONCURRENT = 10

# Type for the per-tool execution function.
# Signature: (ToolCallInfo) -> Awaitable[ToolResultEnvelope]
ExecuteFn = Callable[[ToolCallInfo], Awaitable[ToolResultEnvelope]]


@dataclass(frozen=True)
class ToolBatch:
    """Partitioned tool batch for concurrent execution."""

    concurrent: tuple[ToolCallInfo, ...] = field(default_factory=tuple)
    sequential: tuple[ToolCallInfo, ...] = field(default_factory=tuple)


def partition_tool_calls(
    calls: list[ToolCallInfo],
    registry: ToolRegistry,
) -> ToolBatch:
    """Classify tool calls as concurrent-safe or sequential.

    Tools with ``side_effects`` of ``"none"`` or ``"read"`` are safe to
    run concurrently.  Tools with ``"write"`` (or unknown tools) are
    executed sequentially after the concurrent batch completes.
    """
    concurrent: list[ToolCallInfo] = []
    sequential: list[ToolCallInfo] = []

    for tc in calls:
        spec = registry.get(tc.name)
        if spec is not None and spec.side_effects in ("none", "read"):
            concurrent.append(tc)
        else:
            sequential.append(tc)

    return ToolBatch(
        concurrent=tuple(concurrent),
        sequential=tuple(sequential),
    )


class ConcurrentToolExecutor:
    """Manages concurrent and serial tool execution with abort control.

    Parameters
    ----------
    max_concurrent:
        Maximum parallel read-only tasks (default 10).

    """

    def __init__(self, max_concurrent: int = _MAX_CONCURRENT) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)

    # ------------------------------------------------------------------
    # Streaming execution — start read-only tools during the stream
    # ------------------------------------------------------------------

    def start_streaming(
        self,
        tc: ToolCallInfo,
        execute_fn: ExecuteFn,
    ) -> asyncio.Task[ToolResultEnvelope]:
        """Kick off a single read-only tool immediately, return a Task.

        The returned task can be awaited later in :meth:`execute_batch`
        so the result is ready sooner.
        """

        async def _run() -> ToolResultEnvelope:
            async with self._semaphore:
                return await execute_fn(tc)

        return asyncio.create_task(_run(), name=f"stream-tool-{tc.name}")

    # ------------------------------------------------------------------
    # Batch execution — main entry point
    # ------------------------------------------------------------------

    async def execute_batch(
        self,
        tool_calls: list[ToolCallInfo],
        execute_fn: ExecuteFn,
        registry: ToolRegistry,
        *,
        pre_started: dict[str, asyncio.Task[ToolResultEnvelope]] | None = None,
    ) -> list[ToolResultEnvelope]:
        """Execute a batch of tool calls with concurrency partitioning.

        1. Read-only tools run concurrently (reusing *pre_started* tasks).
        2. Mutation tools run sequentially afterward.
        3. Results are returned in the original *tool_calls* order.
        """
        pre = pre_started or {}
        batch = partition_tool_calls(tool_calls, registry)

        # --- Phase 1: concurrent read-only tools ---
        concurrent_results = await self._run_concurrent(
            list(batch.concurrent),
            execute_fn,
            pre,
        )

        # --- Phase 2: sequential mutation tools ---
        sequential_results = await self._run_sequential(
            list(batch.sequential),
            execute_fn,
        )

        # --- Merge results in original order ---
        result_map: dict[str, ToolResultEnvelope] = {}
        for r in concurrent_results:
            result_map[r.call_id] = r
        for r in sequential_results:
            result_map[r.call_id] = r

        ordered: list[ToolResultEnvelope] = []
        for tc in tool_calls:
            if tc.tool_use_id in result_map:
                ordered.append(result_map[tc.tool_use_id])
            else:
                # Shouldn't happen, but guard against it
                ordered.append(
                    ToolResultEnvelope(
                        call_id=tc.tool_use_id,
                        tool=tc.name,
                        status="error",
                        error=ToolExecutionError(
                            type=ToolErrorType.UNKNOWN,
                            message="Tool result missing after execution",
                        ),
                        tool_use_id=tc.tool_use_id,
                    ),
                )
        return ordered

    # ------------------------------------------------------------------
    # Internal: concurrent phase with sibling abort
    # ------------------------------------------------------------------

    async def _run_concurrent(
        self,
        calls: list[ToolCallInfo],
        execute_fn: ExecuteFn,
        pre_started: dict[str, asyncio.Task[ToolResultEnvelope]],
    ) -> list[ToolResultEnvelope]:
        """Run read-only tools concurrently, aborting siblings on error."""
        if not calls:
            return []

        tasks: dict[str, asyncio.Task[ToolResultEnvelope]] = {}

        for tc in calls:
            existing = pre_started.get(tc.tool_use_id)
            if existing is not None and not existing.cancelled():
                tasks[tc.tool_use_id] = existing
            else:
                tasks[tc.tool_use_id] = self.start_streaming(tc, execute_fn)

        # Wait for all with sibling abort on first error
        results: list[ToolResultEnvelope] = []
        pending: set[asyncio.Task[ToolResultEnvelope]] = set(tasks.values())

        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                try:
                    result = task.result()
                except asyncio.CancelledError:
                    # Find the ToolCallInfo for this cancelled task
                    tc_id = _find_task_id(tasks, task)
                    results.append(
                        ToolResultEnvelope(
                            call_id=tc_id,
                            tool="unknown",
                            status="error",
                            error=ToolExecutionError(
                                type=ToolErrorType.UNKNOWN,
                                message="Cancelled: sibling tool failed",
                                safe_to_retry=True,
                            ),
                            tool_use_id=tc_id,
                        ),
                    )
                    continue
                except Exception as exc:
                    tc_id = _find_task_id(tasks, task)
                    results.append(
                        ToolResultEnvelope(
                            call_id=tc_id,
                            tool="unknown",
                            status="error",
                            error=ToolExecutionError(
                                type=ToolErrorType.UNKNOWN,
                                message=str(exc),
                            ),
                            tool_use_id=tc_id,
                        ),
                    )
                    # Abort siblings
                    logger.info(
                        "Tool task failed, cancelling %d sibling tasks",
                        len(pending),
                    )
                    for p in pending:
                        p.cancel()
                    # Collect cancelled results
                    if pending:
                        cancelled_done, _ = await asyncio.wait(pending)
                        for ct in cancelled_done:
                            ct_id = _find_task_id(tasks, ct)
                            try:
                                results.append(ct.result())
                            except (asyncio.CancelledError, Exception):
                                results.append(
                                    ToolResultEnvelope(
                                        call_id=ct_id,
                                        tool="unknown",
                                        status="error",
                                        error=ToolExecutionError(
                                            type=ToolErrorType.UNKNOWN,
                                            message="Cancelled: sibling tool failed",
                                            safe_to_retry=True,
                                        ),
                                        tool_use_id=ct_id,
                                    ),
                                )
                    pending = set()
                    continue

                # Success — check if result itself reports an error
                if result.status == "error":
                    # Abort siblings on tool-level errors too
                    logger.info(
                        "Tool %s returned error, cancelling %d siblings",
                        result.tool,
                        len(pending),
                    )
                    results.append(result)
                    for p in pending:
                        p.cancel()
                    if pending:
                        cancelled_done, _ = await asyncio.wait(pending)
                        for ct in cancelled_done:
                            ct_id = _find_task_id(tasks, ct)
                            try:
                                results.append(ct.result())
                            except (asyncio.CancelledError, Exception):
                                results.append(
                                    ToolResultEnvelope(
                                        call_id=ct_id,
                                        tool="unknown",
                                        status="error",
                                        error=ToolExecutionError(
                                            type=ToolErrorType.UNKNOWN,
                                            message="Cancelled: sibling tool failed",
                                            safe_to_retry=True,
                                        ),
                                        tool_use_id=ct_id,
                                    ),
                                )
                    pending = set()
                    continue

                results.append(result)

        return results

    # ------------------------------------------------------------------
    # Internal: sequential phase
    # ------------------------------------------------------------------

    async def _run_sequential(
        self,
        calls: list[ToolCallInfo],
        execute_fn: ExecuteFn,
    ) -> list[ToolResultEnvelope]:
        """Run mutation tools one at a time."""
        results: list[ToolResultEnvelope] = []
        for tc in calls:
            result = await execute_fn(tc)
            results.append(result)
        return results


def _find_task_id(
    tasks: dict[str, asyncio.Task[Any]],
    target: asyncio.Task[Any],
) -> str:
    """Reverse-lookup a task's call_id from the tasks dict."""
    for tid, task in tasks.items():
        if task is target:
            return tid
    return "unknown"


__all__ = [
    "ConcurrentToolExecutor",
    "ExecuteFn",
    "ToolBatch",
    "partition_tool_calls",
]
