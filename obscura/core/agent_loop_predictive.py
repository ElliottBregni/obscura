"""obscura.core.agent_loop_predictive — Predictive cache wiring for AgentLoopV2.

When the model emits text like "Let me read foo.py", v1 speculatively
dispatched ``read_text_file({"path": "foo.py"})`` while the model continued
streaming. By the time the model actually emits the tool_use block, the
result is already in the cache and the dispatch resolves instantly.

This module ports that to v2:

- :class:`V2PredictiveCache` — stores ``asyncio.Task[list[ContentBlock]]``
  keyed by ``(tool_name, args)``. Tasks are awaited on cache hit, cancelled
  on turn-end clear, evicted oldest-first when full.
- :func:`predictive_cache_middleware` — dispatch middleware that checks
  the cache before invoking inner; cache hit returns immediately.
- :func:`make_predictive_observer` — TEXT_DELTA observer that feeds
  :class:`ToolPredictor` and starts speculative dispatches for read-only
  predictions.

Strict safety: speculation only fires for tools where
``ToolSpec.side_effects == "none"``. Anything that writes, calls
networks, runs shell commands, etc. is never speculated.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from obscura.core.tool_context import bind_tool_context
from obscura.core.types import ContentBlock

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from obscura.core.dag import DAGNode
    from obscura.core.tool_context import ToolContext
    from obscura.core.tools import ToolRegistry
    from obscura.runtime.predictive_tools import ToolPredictor


logger = logging.getLogger(__name__)


__all__ = [
    "V2PredictiveCache",
    "make_predictive_observer",
    "predictive_cache_middleware",
]


# ---------------------------------------------------------------------------
# V2-specific cache (stores list[ContentBlock] tasks; v1 uses ToolResultEnvelope)
# ---------------------------------------------------------------------------

# Speculative cache keys + entries are keyed on (tool_name, args) where
# args is the heterogeneous JSON arg shape of arbitrary tools. The cache
# is generic across every registered ToolSpec, so ``dict[str, Any]`` is
# the legitimate shape here — per-tool typing happens inside each tool
# handler, not at the cache layer.


@dataclass
class _Entry:
    key: str
    tool: str
    args: dict[str, Any]
    task: asyncio.Task[list[ContentBlock]]


class V2PredictiveCache:
    """Hold speculatively-executed tool results for one turn.

    Per-turn — :meth:`clear` is called between turns so stale predictions
    don't leak across model context boundaries.
    """

    def __init__(self, max_entries: int = 10) -> None:
        self._entries: dict[str, _Entry] = {}
        self._max_entries = max_entries
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _key(name: str, args: dict[str, Any]) -> str:
        try:
            args_json = json.dumps(args, sort_keys=True, default=str)
        except TypeError:
            # Args contain something json can't even default-stringify
            # (a circular ref, a sentinel object, etc.). Fall back to a
            # repr-based key — collisions are fine, dedup is best-effort.
            logger.debug("predictive cache key fell back to repr", exc_info=True)
            args_json = repr(sorted(args.items()))
        return f"{name}::{args_json}"

    def has(self, name: str, args: dict[str, Any]) -> bool:
        return self._key(name, args) in self._entries

    def put(
        self,
        name: str,
        args: dict[str, Any],
        task: asyncio.Task[list[ContentBlock]],
    ) -> None:
        """Stash a speculative task. Evicts oldest entry if at capacity."""
        key = self._key(name, args)
        if key in self._entries:
            return  # already speculating
        if len(self._entries) >= self._max_entries:
            # Evict oldest by insertion order — simple and good enough.
            oldest_key = next(iter(self._entries))
            evicted = self._entries.pop(oldest_key)
            evicted.task.cancel()
            logger.debug("V2PredictiveCache evicted: %s (cache full)", evicted.tool)
        self._entries[key] = _Entry(key=key, tool=name, args=args, task=task)

    async def pop_and_await(
        self,
        name: str,
        args: dict[str, Any],
    ) -> list[ContentBlock] | None:
        """Cache-hit lookup. Returns content blocks or None on miss / failure.

        On hit, the entry is removed (predictive cache is consume-once
        within a turn — the model only sends each tool_use once).
        """
        key = self._key(name, args)
        entry = self._entries.pop(key, None)
        if entry is None:
            self._misses += 1
            return None
        self._hits += 1
        try:
            return await entry.task
        except asyncio.CancelledError:
            logger.debug("V2PredictiveCache: speculation cancelled for %s", name)
            raise
        except Exception:
            logger.debug(
                "V2PredictiveCache: speculation raised for %s, falling back",
                name,
                exc_info=True,
            )
            return None

    def clear(self) -> None:
        """Cancel all in-flight speculations and reset."""
        for entry in self._entries.values():
            if not entry.task.done():
                entry.task.cancel()
        self._entries.clear()

    @property
    def stats(self) -> dict[str, int]:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "pending": len(self._entries),
        }


# ---------------------------------------------------------------------------
# Dispatch middleware
# ---------------------------------------------------------------------------


def predictive_cache_middleware(
    cache: V2PredictiveCache,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Dispatch middleware that consumes :class:`V2PredictiveCache` hits.

    On cache hit, returns the speculative result immediately without
    invoking ``inner``. On miss (or speculation failure), falls through
    to ``inner`` and the tool runs normally.
    """

    def wrap(inner: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapped(
            node: DAGNode,
            resolved: dict[str, Any],
        ) -> list[ContentBlock]:
            cached = await cache.pop_and_await(node.tool_name, node.tool_input)
            if cached is not None:
                return cached
            return await inner(node, resolved)

        return wrapped

    return wrap


# ---------------------------------------------------------------------------
# Stream-level observer
# ---------------------------------------------------------------------------


def make_predictive_observer(
    *,
    predictor: ToolPredictor,
    cache: V2PredictiveCache,
    registry: ToolRegistry,
    tool_ctx: ToolContext,
) -> Callable[[str], Awaitable[None]]:
    """Build a TEXT_DELTA observer that fires speculative dispatches.

    Each delta is fed to *predictor*; resulting predictions are filtered
    to read-only tools and dispatched as ``asyncio.Task`` objects stored
    in *cache*. The middleware retrieves them when the model actually
    emits the corresponding tool_use block.

    Speculation fires only when:

    - ``predictor.predict()`` returns a non-empty list
    - the predicted tool exists in *registry*
    - ``ToolSpec.side_effects == "none"`` (read-only)
    - the same ``(tool, args)`` is not already in the cache
    """

    async def observer(delta_text: str) -> None:
        if not delta_text:
            return
        predictor.feed(delta_text)
        for pred in predictor.predict():
            spec = registry.get(pred.tool)
            if spec is None:
                continue
            if getattr(spec, "side_effects", "") != "none":
                # Hard safety: never speculate side-effecting tools.
                continue
            if cache.has(pred.tool, pred.args):
                continue
            task = asyncio.create_task(
                _run_speculation(spec, pred.args, tool_ctx, pred.tool)
            )
            cache.put(pred.tool, pred.args, task)
            logger.debug(
                "predictive: speculating %s(%s) confidence=%.2f",
                pred.tool,
                pred.args,
                pred.confidence,
            )

    return observer


async def _run_speculation(
    spec: Any,
    args: dict[str, Any],
    ctx: ToolContext,
    name: str,
) -> list[ContentBlock]:
    """Speculative tool invocation. Runs in a fresh task, may be cancelled."""
    try:
        with bind_tool_context(ctx):
            result = spec.handler(**args)
            if asyncio.iscoroutine(result):
                result = await result
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug(
            "predictive: speculation for %s raised — caller will re-execute",
            name,
            exc_info=True,
        )
        return []
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    return [ContentBlock(kind="text", text=text)]
