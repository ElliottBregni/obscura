"""obscura.core.predictive_tools — Speculative pre-execution of read-only tools.

While the model is streaming text, this module analyzes the partial output
to predict which tools the model is about to call.  Read-only tools that
match a prediction are pre-executed in the background so results are ready
by the time the model's actual tool_use block arrives.

Architecture:

1. **PredictiveToolCache** — holds prefetched results keyed by
   ``(tool_name, frozenset(args.items()))``.
2. **ToolPredictor** — analyzes streaming text to produce predictions.
   Uses lightweight regex/keyword matching (no extra model calls).
3. **Integration** — the agent loop feeds text deltas into the predictor
   during streaming, and checks the cache before executing tool calls.

Only concurrency-safe (``side_effects="none"``) tools are eligible for
speculative execution.  Cache entries expire after one turn.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.core.types import ToolResultEnvelope, ToolSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _cache_key(name: str, args: dict[str, Any]) -> str:
    """Deterministic cache key for a tool call."""
    args_json = json.dumps(args, sort_keys=True, default=str)
    h = hashlib.sha256(args_json.encode()).hexdigest()[:16]
    return f"{name}|{h}"


@dataclass
class CacheEntry:
    """A single prefetched tool result."""

    key: str
    tool: str
    args: dict[str, Any]
    task: asyncio.Task[ToolResultEnvelope]
    created_at: float = field(default_factory=time.monotonic)


class PredictiveToolCache:
    """Hold speculatively-executed tool results for one turn.

    Thread-safe via asyncio (single event loop, no threading).
    """

    def __init__(self, max_entries: int = 10) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._max_entries = max_entries
        self._hits = 0
        self._misses = 0

    def has(self, name: str, args: dict[str, Any]) -> bool:
        return _cache_key(name, args) in self._entries

    def put(
        self,
        name: str,
        args: dict[str, Any],
        task: asyncio.Task[ToolResultEnvelope],
    ) -> None:
        key = _cache_key(name, args)
        if key in self._entries:
            return  # already prefetching
        if len(self._entries) >= self._max_entries:
            # Evict oldest
            oldest_key = min(self._entries, key=lambda k: self._entries[k].created_at)
            evicted = self._entries.pop(oldest_key)
            evicted.task.cancel()
            logger.debug("Predictive cache evicted: %s", evicted.tool)
        self._entries[key] = CacheEntry(key=key, tool=name, args=args, task=task)
        logger.debug("Predictive prefetch started: %s", name)

    async def get(self, name: str, args: dict[str, Any]) -> ToolResultEnvelope | None:
        """Retrieve a prefetched result, awaiting the task if needed.

        Returns ``None`` on cache miss.
        """
        key = _cache_key(name, args)
        entry = self._entries.pop(key, None)
        if entry is None:
            self._misses += 1
            return None
        self._hits += 1
        try:
            return await entry.task
        except Exception:
            logger.debug("Predictive prefetch failed for %s, will re-execute", name)
            return None

    def clear(self) -> None:
        """Cancel all in-flight prefetches and reset."""
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
# Prediction patterns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolPrediction:
    """A predicted tool call with estimated args."""

    tool: str
    args: dict[str, Any]
    confidence: float  # 0.0 - 1.0


# Patterns: model text → likely tool call.
# Each entry: (compiled_regex, tool_name, arg_extractor_fn)
_PatternEntry = tuple[re.Pattern[str], str, Callable[[re.Match[str]], dict[str, Any]]]
_PREDICTION_PATTERNS: list[_PatternEntry] = []


def _init_patterns() -> None:
    """Build the pattern table once at import time."""
    global _PREDICTION_PATTERNS

    def _path_arg(m: re.Match[str]) -> dict[str, Any]:
        return {"path": m.group("path").strip("`'\"")}

    def _pattern_arg(m: re.Match[str]) -> dict[str, Any]:
        return {"pattern": m.group("pat").strip("`'\"")}

    def _query_arg(m: re.Match[str]) -> dict[str, Any]:
        return {"query": m.group("q").strip("`'\"")}

    _PREDICTION_PATTERNS.extend(
        [
            # "Let me read <path>" / "I'll check <path>" / "Looking at <path>"
            (
                re.compile(
                    r"(?:let me |I'?ll |I need to |going to |looking at )"
                    r"(?:read|check|look at|open|view|inspect|examine)\s+"
                    r"(?:the\s+)?(?:file\s+(?:at\s+)?)?[`'\"]?"
                    r"(?P<path>(?:[/\w][\w/.\-]*)?[\w\-]+\.\w+)[`'\"]?",
                    re.IGNORECASE,
                ),
                "read_text_file",
                _path_arg,
            ),
            # "Let me search for <pattern>" / "grep for <pattern>"
            (
                re.compile(
                    r"(?:let me |I'?ll |going to )"
                    r"(?:search|grep|find|look) (?:for )?[`'\"]?(?P<pat>[^\n`'\"]{3,60})[`'\"]?",
                    re.IGNORECASE,
                ),
                "grep_files",
                _pattern_arg,
            ),
            # "Let me find files matching <glob>"
            (
                re.compile(
                    r"(?:let me |I'?ll |going to )"
                    r"(?:find files|list files|glob|find .*files)"
                    r"[^`\n]*[`'\"]?(?P<pat>\*\*?[/\w.*]+)[`'\"]?",
                    re.IGNORECASE,
                ),
                "find_files",
                _pattern_arg,
            ),
            # "Let me check the git status/log/diff"
            (
                re.compile(
                    r"(?:let me |I'?ll |going to )"
                    r"(?:check|run|look at) (?:the )?git (?P<q>status|log|diff)",
                    re.IGNORECASE,
                ),
                "git",
                lambda m: {"action": m.group("q").strip()},
            ),
            # "Let me search the web/memory for <query>"
            (
                re.compile(
                    r"(?:let me |I'?ll |going to )"
                    r"(?:search (?:the )?(?:web|memory|vector memory) for )"
                    r"[`'\"]?(?P<q>[^\n`'\"]{3,80})[`'\"]?",
                    re.IGNORECASE,
                ),
                "semantic_search",
                _query_arg,
            ),
        ]
    )


_init_patterns()


# ---------------------------------------------------------------------------
# Predictor
# ---------------------------------------------------------------------------


class ToolPredictor:
    """Analyzes streaming text to predict upcoming tool calls.

    Call :meth:`feed` with text deltas.  Call :meth:`predict` to get
    the current set of predictions.  Call :meth:`reset` between turns.
    """

    def __init__(self, tool_registry: dict[str, ToolSpec] | None = None) -> None:
        self._buffer: str = ""
        self._tools = tool_registry or {}
        self._already_predicted: set[str] = set()  # cache keys already fired
        # Minimum text length before we start predicting
        self._min_buffer = 20

    def feed(self, text: str) -> None:
        """Append a text delta to the analysis buffer."""
        self._buffer += text

    def predict(self) -> list[ToolPrediction]:
        """Return predicted tool calls based on accumulated text.

        Only returns predictions for tools that:
        1. Exist in the registry
        2. Are concurrency-safe (no side effects)
        3. Haven't been predicted yet this turn
        """
        if len(self._buffer) < self._min_buffer:
            return []

        predictions: list[ToolPrediction] = []
        # Only analyze the last 500 chars (the recent text is most predictive)
        window = self._buffer[-500:]

        for pattern, tool_name, arg_fn in _PREDICTION_PATTERNS:
            # Check tool exists and is safe
            spec = self._tools.get(tool_name)
            if spec is None or not spec.is_concurrency_safe():
                continue

            for m in pattern.finditer(window):
                try:
                    args = arg_fn(m)
                except (IndexError, AttributeError):
                    logger.debug("suppressed exception in predict", exc_info=True)
                    continue

                key = _cache_key(tool_name, args)
                if key in self._already_predicted:
                    continue
                self._already_predicted.add(key)

                predictions.append(
                    ToolPrediction(
                        tool=tool_name,
                        args=args,
                        confidence=0.8,
                    ),
                )

        return predictions

    def reset(self) -> None:
        """Clear state between turns."""
        self._buffer = ""
        self._already_predicted.clear()
