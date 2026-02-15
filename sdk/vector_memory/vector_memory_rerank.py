"""
sdk/vector_memory_rerank — Reranking functions for two-stage retrieval.

Stage 2 rerankers score candidates from the initial vector search
using additional signals like term overlap, recency, and metadata.

Usage::

    from sdk.vector_memory_rerank import RecencyReranker, CompositeReranker

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
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sdk.vector_memory import VectorMemoryEntry


@dataclass(frozen=True, slots=True)
class RerankRequest:
    query: str
    entry: "VectorMemoryEntry"
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
    """
    BM25-style term frequency reranker.

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
    """
    Boost recent memories with exponential decay.

    Score = exp(-age_days / decay_days) * weight
    """

    decay_days: float = 30.0
    weight: float = 1.0

    def score(
        self,
        query: str,
        entry: VectorMemoryEntry,
        query_embedding: list[float],
    ) -> float:
        now = datetime.now(UTC)
        created = entry.created_at
        # Handle naive datetimes
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age_days = max((now - created).total_seconds() / 86400, 0)
        return math.exp(-age_days / self.decay_days) * self.weight


@dataclass
class MetadataReranker:
    """
    Boost based on metadata signals.

    Give bonus points when specific metadata keys are present and truthy.
    """

    boost_keys: dict[str, float] = field(default_factory=lambda: dict[str, float]())

    def score(
        self,
        query: str,
        entry: VectorMemoryEntry,
        query_embedding: list[float],
    ) -> float:
        total = 0.0
        for key, boost in self.boost_keys.items():
            if key in entry.metadata and entry.metadata[key]:
                total += boost
        return total


@dataclass
class CompositeReranker:
    """
    Combine multiple rerankers with weights.

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
