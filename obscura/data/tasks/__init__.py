"""obscura.data.tasks — work queue with claim semantics.

Phase 3b of the data-layer migration. Implementation moved from
``obscura.core.task_queue``; that module is now a thin re-export shim.

Public API:

* :class:`TaskRepo` — Protocol every backend implements (``enqueue``,
  ``next_ready``, ``claim``, ``release``, ``heartbeat``, ``complete``,
  ``fail``, ``queue_depth``, ``get``, ``list_claimed``, ``reclaim_stale``)
* :func:`get_task_repo` — factory; SQLite-only this turn (Postgres in
  Phase 3c)

The repo handles WAL-mode SQLite + per-call connections internally — no
context manager required by callers, just instantiate and use.
"""

from __future__ import annotations

from obscura.data.tasks.factory import (
    get_task_repo as get_task_repo,
)
from obscura.data.tasks.protocol import (
    TaskRepo as TaskRepo,
)
