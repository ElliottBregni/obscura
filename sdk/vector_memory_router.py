"""
sdk/vector_memory_router — Memory type routing for multi-query retrieval.

Routes a single query across multiple memory types with configurable weights,
then merges and deduplicates results.

Usage::

    from sdk.vector_memory_router import MemoryRouter, MemoryTypeQuery

    router = MemoryRouter(store)
    result = router.route_and_merge(
        query="how to handle async?",
        routes=[
            MemoryTypeQuery("fact", weight=0.4, top_k=15),
            MemoryTypeQuery("episode", weight=0.3, top_k=10),
            MemoryTypeQuery("summary", weight=0.3, top_k=10),
        ],
        final_top_k=10,
    )
    for entry in result.entries:
        print(entry.text, entry.final_score)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sdk.vector_memory import VectorMemoryEntry, VectorMemoryStore
    from sdk.vector_memory_rerank import Reranker
    from sdk.vector_memory_filters import MetadataFilter
else:
    from sdk.vector_memory_filters import MetadataFilter


@dataclass
class MemoryTypeQuery:
    """Configuration for querying a specific memory type."""
    memory_type: str
    weight: float = 1.0
    top_k: int = 10
    reranker: Reranker | None = None


@dataclass
class RoutedResult:
    """Result from a routed multi-type search."""
    entries: list[VectorMemoryEntry]
    sources: dict[str, int]  # memory_type -> count of results from that type


class MemoryRouter:
    """Route queries to different memory types and merge results."""

    def __init__(self, store: VectorMemoryStore):
        self.store = store

    def route_and_merge(
        self,
        query: str,
        routes: list[MemoryTypeQuery],
        final_top_k: int = 10,
        namespace: str | None = None,
        threshold: float = -1.0,
        first_stage_k: int = 50,
        metadata_filters: list[MetadataFilter] | None = None,
    ) -> RoutedResult:
        """
        Execute separate queries per memory type and merge results.

        Each route searches its memory type with its own weight and top_k,
        then results are weighted, deduplicated, and merged.

        Args:
            query: The search query
            routes: List of MemoryTypeQuery configs
            final_top_k: How many results to return after merging
            namespace: Filter by namespace (applied to all routes)
            threshold: Minimum similarity score
            first_stage_k: Candidate pool size per route
            metadata_filters: Additional filters applied to all routes
        """
        all_results: list[VectorMemoryEntry] = []
        sources: dict[str, int] = {}

        for route in routes:
            results = self.store.search_reranked(
                query=query,
                namespace=namespace,
                top_k=route.top_k,
                first_stage_k=first_stage_k,
                threshold=threshold,
                memory_types=[route.memory_type],
                metadata_filters=metadata_filters,
                reranker=route.reranker,
            )

            # Apply route weight to final scores
            for entry in results:
                entry.final_score *= route.weight

            all_results.extend(results)
            sources[route.memory_type] = len(results)

        merged = self._dedupe_and_sort(all_results)
        return RoutedResult(
            entries=merged[:final_top_k],
            sources=sources,
        )

    @staticmethod
    def _dedupe_and_sort(results: list[VectorMemoryEntry]) -> list[VectorMemoryEntry]:
        """Deduplicate by (namespace, key), keeping highest final_score."""
        seen: dict[tuple[str, str], VectorMemoryEntry] = {}
        for entry in results:
            composite_key = (entry.key.namespace, entry.key.key)
            existing = seen.get(composite_key)
            if existing is None or entry.final_score > existing.final_score:
                seen[composite_key] = entry

        merged = list(seen.values())
        merged.sort(key=lambda x: x.final_score, reverse=True)
        return merged
