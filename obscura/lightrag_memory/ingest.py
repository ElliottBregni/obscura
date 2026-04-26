"""obscura.lightrag_memory.ingest — Async write-path helpers.

Phase 1 stub. Phase 2 will add helpers that:

- chunk long text before handing it to LightRAG,
- batch concurrent inserts with bounded concurrency,
- consult ``LightRAGAdapter.indexable_types`` to skip non-indexable memory_types.
"""

from __future__ import annotations
