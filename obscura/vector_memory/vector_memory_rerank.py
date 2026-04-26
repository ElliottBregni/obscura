"""sdk/vector_memory_rerank — Reranking functions for two-stage retrieval.

Stage 2 rerankers score candidates from the initial vector search
using additional signals like term overlap, recency, and metadata.

Usage::

    from obscura.vector_memory_rerank import RecencyReranker, CompositeReranker

    reranker = CompositeReranker([
        (RecencyReranker(decay_days=30), 0.6),
        (BM25Reranker(), 0.4),
    ])
    score = reranker.score(query, entry, query_embedding)
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from obscura.vector_memory import VectorMemoryEntry
    from obscura.vector_memory.scoring import HybridWeights


@dataclass(frozen=True, slots=True)
class RerankRequest:
    query: str
    entry: VectorMemoryEntry
    query_embedding: list[float]


@dataclass(frozen=True, slots=True)
class RerankResponse:
    score: float


@runtime_checkable
class Reranker(Protocol):
    """Protocol for second-stage reranking functions."""

    def score(
        self,
        query: str,
        entry: VectorMemoryEntry,
        query_embedding: list[float],
    ) -> float:
        """Return a rerank score (higher is better)."""
        ...


def _tokenize(text: str) -> list[str]:
    """Simple whitespace + punctuation tokenizer."""
    return re.findall(r"\w+", text.lower())


@dataclass
class BM25Reranker:
    """BM25-style term frequency reranker.

    Scores based on query term overlap with the memory text.
    No external model required — pure term matching.
    """

    k1: float = 1.5
    b: float = 0.75

    def score(
        self,
        query: str,
        entry: VectorMemoryEntry,
        query_embedding: list[float],
    ) -> float:
        query_tokens = _tokenize(query)
        doc_tokens = _tokenize(entry.text)

        if not query_tokens or not doc_tokens:
            return 0.0

        doc_len = len(doc_tokens)
        avg_dl = max(doc_len, 1)  # single-doc approximation
        tf = Counter(doc_tokens)

        total = 0.0
        for term in query_tokens:
            freq = tf.get(term, 0)
            if freq == 0:
                continue
            numerator = freq * (self.k1 + 1)
            denominator = freq + self.k1 * (1 - self.b + self.b * doc_len / avg_dl)
            total += numerator / denominator

        # Normalize to roughly [0, 1] range
        max_possible = len(query_tokens) * (self.k1 + 1)
        return total / max_possible if max_possible > 0 else 0.0


@dataclass
class RecencyReranker:
    """Hybrid reranker blending vector similarity, decay, and usage.

    Internally calls :func:`~obscura.vector_memory.scoring.hybrid_score`
    with the configured :class:`HybridWeights`.  The graph term is left
    at 0 by default (forward compat for a future graph-retrieval layer).

    When a :class:`~obscura.vector_memory.decay.DecayConfig` is provided
    (the common case), the decay component is computed via
    :func:`~obscura.vector_memory.decay.compute_decay` for per-type
    half-lives and ``accessed_at`` boost.  Otherwise falls back to a
    simple ``exp(-age / decay_days)`` formula for the decay term.
    """

    decay_days: float = 30.0
    weight: float = 1.0
    decay_config: Any | None = None  # Optional DecayConfig
    weights: HybridWeights | None = None

    def __post_init__(self) -> None:
        if self.weights is None:
            from obscura.vector_memory.scoring import load_hybrid_weights_from_disk

            self.weights = load_hybrid_weights_from_disk()

    def score(
        self,
        query: str,
        entry: VectorMemoryEntry,
        query_embedding: list[float],
    ) -> float:
        from obscura.vector_memory.scoring import HybridWeights, hybrid_score

        if self.decay_config is not None:
            from obscura.vector_memory.decay import compute_decay

            accessed_at = getattr(entry, "accessed_at", None)
            decay = compute_decay(
                entry.memory_type,
                entry.created_at,
                accessed_at,
                self.decay_config,
            )
        else:
            now = datetime.now(UTC)
            created = entry.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age_days = max((now - created).total_seconds() / 86400, 0)
            decay = math.exp(-age_days / self.decay_days)

        vector_sim = entry.score or 0.0
        usage_count = int(entry.metadata.get("access_count") or 0)
        weights = self.weights if self.weights is not None else HybridWeights()

        return (
            hybrid_score(
                vector_sim=vector_sim,
                decay_multiplier=decay,
                usage_count=usage_count,
                graph_relevance=0.0,
                weights=weights,
            )
            * self.weight
        )


@dataclass
class MetadataReranker:
    """Boost based on metadata signals.

    Give bonus points when specific metadata keys are present and truthy.
    """

    boost_keys: dict[str, float] = field(default_factory=dict[str, float])

    def score(
        self,
        query: str,
        entry: VectorMemoryEntry,
        query_embedding: list[float],
    ) -> float:
        total = 0.0
        for key, boost in self.boost_keys.items():
            if entry.metadata.get(key):
                total += boost
        return total


@dataclass
class CompositeReranker:
    """Combine multiple rerankers with weights.

    Example::

        reranker = CompositeReranker([
            (RecencyReranker(decay_days=30), 0.5),
            (BM25Reranker(), 0.3),
            (MetadataReranker(boost_keys={"important": 0.5}), 0.2),
        ])
    """

    rerankers: list[tuple[Reranker, float]]

    def score(
        self,
        query: str,
        entry: VectorMemoryEntry,
        query_embedding: list[float],
    ) -> float:
        total = 0.0
        for reranker, weight in self.rerankers:
            total += reranker.score(query, entry, query_embedding) * weight
        return total
