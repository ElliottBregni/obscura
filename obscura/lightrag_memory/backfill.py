"""obscura.lightrag_memory.backfill — Batch migration of existing vector chunks.

Phase 1 stub. Phase 5 will add:

- a CLI subcommand entry (``obscura memory backfill-graph``),
- iteration over ``backend.list_keys`` with rate limiting,
- idempotency via a ``lr_indexed_at`` metadata flag.
"""

from __future__ import annotations
