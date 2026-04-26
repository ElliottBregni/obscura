"""obscura.lightrag_memory.backfill — Batch + lazy graph backfill.

Library-callable engine: the CLI is a thin wrapper; future API endpoints /
supervisor tasks can call ``BackfillEngine.run()`` directly.

Phase 5 concerns:
- Walk the canonical store, identify chunks that are eligible for graph
  indexing but lack ``lr_indexed_at``.
- Estimate LLM cost up-front (gpt-4o-mini default) so operators can decide
  before burning money.
- Drive ``LightRAGAdapter.insert_safe`` at a configurable rate, marking each
  successful chunk with ``lr_indexed_at`` so re-runs are idempotent.
- Track per-chunk failure attempts in ``lr_index_attempts`` so the lazy-
  on-touch path can stop retrying after 3 failures.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser
    from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
    from obscura.vector_memory.backends.base import VectorEntry

_log = logging.getLogger(__name__)


AVG_TOKENS_PER_CHUNK = 800
LLM_CALLS_PER_CHUNK = 4
COST_PER_1K_INPUT_TOKENS = 0.000150
COST_PER_1K_OUTPUT_TOKENS = 0.000600
DEFAULT_OUTPUT_RATIO = 0.25

MIN_CHUNK_CHARS = 50


@dataclass(frozen=True)
class BackfillConfig:
    """User-facing knobs. Mirrors the CLI flags 1:1."""

    namespace: str | None = None
    memory_types: frozenset[str] | None = None
    batch_size: int = 50
    rate_limit: float = 1.0
    max_chunks: int | None = None
    dry_run: bool = False
    retry_failed: bool = False
    include_episodes: bool = False
    log_file: Path | None = None


@dataclass
class BackfillEstimate:
    """Output of the discovery + cost-estimation phase."""

    total_chunks: int = 0
    by_memory_type: dict[str, int] = field(default_factory=dict)
    estimated_llm_calls: int = 0
    estimated_cost_usd: float = 0.0
    estimated_duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BackfillReport:
    """Output of a completed (or partially-completed) run."""

    chunks_indexed: int = 0
    chunks_skipped: int = 0
    chunks_failed: int = 0
    duration_seconds: float = 0.0
    actual_llm_calls: int = 0
    actual_cost_usd: float = 0.0
    failed_keys: list[tuple[str, str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BackfillEngine:
    """Library-callable batch backfill.

    Usage::

        engine = BackfillEngine(store, config)
        estimate = engine.estimate()
        if not config.dry_run:
            report = engine.run(on_progress=tqdm_callback)
    """

    def __init__(
        self,
        store: HybridVectorMemoryStore,
        config: BackfillConfig,
    ) -> None:
        self.store = store
        self.config = config
        self._todo: list[VectorEntry] = []
        self._discovered = False

    def estimate(self) -> BackfillEstimate:
        """Walk the backend, decide which chunks to index, and estimate cost."""
        self._discover()
        by_type: dict[str, int] = {}
        for entry in self._todo:
            by_type[entry.memory_type] = by_type.get(entry.memory_type, 0) + 1
        total = len(self._todo)
        llm_calls = total * LLM_CALLS_PER_CHUNK
        input_tokens = total * AVG_TOKENS_PER_CHUNK
        output_tokens = int(input_tokens * DEFAULT_OUTPUT_RATIO)
        cost = (
            input_tokens / 1000 * COST_PER_1K_INPUT_TOKENS
            + output_tokens / 1000 * COST_PER_1K_OUTPUT_TOKENS
        )
        duration = total / max(self.config.rate_limit, 0.01)
        return BackfillEstimate(
            total_chunks=total,
            by_memory_type=by_type,
            estimated_llm_calls=llm_calls,
            estimated_cost_usd=cost,
            estimated_duration_seconds=duration,
        )

    def _discover(self) -> None:
        if self._discovered:
            return
        store = self.store
        cfg = self.config

        indexable = (
            set(cfg.memory_types)
            if cfg.memory_types
            else set(store._lr.indexable_types)
        )
        if cfg.include_episodes:
            indexable.add("episode")

        todo: list[VectorEntry] = []
        for key in store.backend.list_keys(namespace=cfg.namespace):
            if cfg.max_chunks and len(todo) >= cfg.max_chunks:
                break
            entry = store.backend.get_vector(key)
            if entry is None:
                continue
            if entry.memory_type not in indexable:
                continue
            md = entry.metadata or {}
            if md.get("lr_indexed_at"):
                continue
            attempts = md.get("lr_index_attempts", 0)
            if cfg.retry_failed:
                if attempts == 0:
                    continue
            else:
                if attempts >= 3:
                    continue
            if len(entry.text) < MIN_CHUNK_CHARS:
                with contextlib.suppress(Exception):
                    store.backend.update_metadata(
                        key,
                        {"lr_index_skip_reason": "below_min_length"},
                    )
                continue
            todo.append(entry)
        self._todo = todo
        self._discovered = True

    def run(
        self,
        *,
        on_progress: Callable[[BackfillReport], None] | None = None,
    ) -> BackfillReport:
        """Execute the backfill. Synchronous; safe to call from CLI."""
        if self.config.dry_run:
            msg = "BackfillEngine.run() called with dry_run=True"
            raise RuntimeError(msg)
        self._discover()

        report = BackfillReport()
        start = time.monotonic()
        try:
            asyncio.run(self._run_async(report, on_progress))
        finally:
            report.duration_seconds = time.monotonic() - start
        return report

    async def _run_async(
        self,
        report: BackfillReport,
        on_progress: Callable[[BackfillReport], None] | None,
    ) -> None:
        interval = 1.0 / max(self.config.rate_limit, 0.01)
        last_call = 0.0

        for entry in self._todo:
            now = time.monotonic()
            wait = max(0.0, interval - (now - last_call))
            if wait > 0:
                await asyncio.sleep(wait)
            last_call = time.monotonic()

            try:
                await asyncio.to_thread(
                    self.store._lr.insert_safe,
                    doc_id=f"{entry.key.namespace}::{entry.key.key}",
                    text=entry.text,
                    metadata={
                        **(entry.metadata or {}),
                        "memory_type": entry.memory_type,
                        "obscura_key": entry.key.key,
                        "obscura_namespace": entry.key.namespace,
                    },
                )
                with contextlib.suppress(Exception):
                    self.store.backend.update_metadata(
                        entry.key,
                        {
                            "lr_indexed_at": datetime.now(UTC).isoformat(),
                            "lr_index_attempts": 0,
                        },
                    )
                report.chunks_indexed += 1
                report.actual_llm_calls += LLM_CALLS_PER_CHUNK
            except Exception as exc:
                _log.exception(
                    "backfill: failed to index %s::%s",
                    entry.key.namespace,
                    entry.key.key,
                )
                prior = (entry.metadata or {}).get("lr_index_attempts", 0)
                with contextlib.suppress(Exception):
                    self.store.backend.update_metadata(
                        entry.key,
                        {
                            "lr_index_attempts": prior + 1,
                            "lr_index_skip_reason": str(exc)[:200],
                            "lr_index_last_error_at": datetime.now(UTC).isoformat(),
                        },
                    )
                report.chunks_failed += 1
                report.failed_keys.append(
                    (entry.key.namespace, entry.key.key, str(exc)[:200]),
                )
            finally:
                if on_progress:
                    with contextlib.suppress(Exception):
                        on_progress(report)

        report.actual_cost_usd = (
            report.actual_llm_calls
            * AVG_TOKENS_PER_CHUNK
            / LLM_CALLS_PER_CHUNK
            / 1000
            * (
                COST_PER_1K_INPUT_TOKENS
                + COST_PER_1K_OUTPUT_TOKENS * DEFAULT_OUTPUT_RATIO
            )
        )


def _backfill_lock_path(user: AuthenticatedUser) -> Path:
    """Per-user lock file at ``~/.obscura/lightrag/<user_hash>/.backfill.lock``."""
    user_hash = hashlib.sha256(user.user_id.encode()).hexdigest()[:16]
    base = Path.home() / ".obscura" / "lightrag" / user_hash
    base.mkdir(parents=True, exist_ok=True)
    return base / ".backfill.lock"
