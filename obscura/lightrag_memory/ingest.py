"""obscura.lightrag_memory.ingest — Async write-path helpers.

Phase 1 stub. Phase 2's actual fan-out lives in
:mod:`obscura.lightrag_memory.hybrid_store`. This module is a forward
extension point for future helpers that:

- chunk long text before handing it to LightRAG,
- batch concurrent inserts with bounded concurrency beyond the per-store
  ``ThreadPoolExecutor``,
- consult ``LightRAGAdapter.indexable_types`` to skip non-indexable
  memory_types in code paths that don't go through ``HybridVectorMemoryStore``.
"""

from __future__ import annotations
