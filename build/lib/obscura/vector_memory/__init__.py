"""Vector memory package exports."""

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
    "DateRangeFilter",
    "DecayConfig",
    "DecayProfile",
    "FilterBuilder",
    "MaintenanceReport",
    "MemoryConsolidator",
    "MemoryRouter",
    "MemoryTypeQuery",
    "MetadataFilter",
    "RecencyReranker",
    "RerankRequest",
    "RerankResponse",
    "RoutedResult",
    "SemanticMemoryMixin",
    "TagFilter",
    "VectorMemoryEntry",
    "VectorMemoryStore",
    "compute_decay",
    "cosine_similarity",
    "load_decay_config",
    "match_metadata_filters",
    "simple_embedding",
]
