"""
Vector memory package exports.
"""

from obscura.vector_memory.consolidator import MemoryConsolidator
from obscura.vector_memory.decay import (
    DecayConfig,
    DecayProfile,
    compute_decay,
    load_decay_config,
)
from obscura.vector_memory.vector_memory import (
    MaintenanceReport,
    SemanticMemoryMixin,
    VectorMemoryEntry,
    VectorMemoryStore,
    cosine_similarity,
    simple_embedding,
)
from obscura.vector_memory.vector_memory_filters import (
    DateRangeFilter,
    FilterBuilder,
    MetadataFilter,
    TagFilter,
    match_metadata_filters,
)
from obscura.vector_memory.vector_memory_rerank import (
    RecencyReranker,
    RerankRequest,
    RerankResponse,
)
from obscura.vector_memory.vector_memory_router import (
    MemoryRouter,
    MemoryTypeQuery,
    RoutedResult,
)

__all__ = [
    "DecayConfig",
    "DecayProfile",
    "MaintenanceReport",
    "MemoryConsolidator",
    "VectorMemoryStore",
    "VectorMemoryEntry",
    "simple_embedding",
    "cosine_similarity",
    "compute_decay",
    "load_decay_config",
    "SemanticMemoryMixin",
    "MetadataFilter",
    "DateRangeFilter",
    "TagFilter",
    "FilterBuilder",
    "match_metadata_filters",
    "RerankRequest",
    "RerankResponse",
    "RecencyReranker",
    "MemoryRouter",
    "MemoryTypeQuery",
    "RoutedResult",
]
