"""obscura.core.task_queue — back-compat shim.

The implementation moved to :mod:`obscura.data.tasks` as part of the
data-layer migration (Phase 3b). This module re-exports the public
surface under its historical names so the existing 6 consumers don't
have to flip their imports today. New code should import from
``obscura.data.tasks`` directly.

Migration plan:
* Phase 3b (this commit) — move + shim, no caller changes
* Phase 3c — Postgres backend + factory env routing
* Later cleanup pass deletes this shim once consumers migrate
"""

from __future__ import annotations

from obscura.data.tasks.factory import get_task_repo as get_task_repo
from obscura.data.tasks.protocol import TaskRepo as TaskRepo
from obscura.data.tasks.sqlite import (
    DEFAULT_CLAIM_TIMEOUT as _CLAIM_TIMEOUT,  # noqa: F401  # legacy name
)
from obscura.data.tasks.sqlite import (
    SqliteTaskRepo as TaskQueue,
)

# Several callers reach into the legacy private connection / path
# helpers to share the WAL + busy-timeout setup, or to monkeypatch the
# DB location in tests. Re-export so they keep working until those
# callers migrate to the repo's own primitives.
from obscura.data.tasks.sqlite import (  # noqa: I001
    _db_path as _db_path,  # pyright: ignore[reportPrivateUsage]
)
from obscura.data.tasks.sqlite import (  # noqa: I001
    _open as _open,  # pyright: ignore[reportPrivateUsage]
)

__all__ = [
    "TaskQueue",
    "TaskRepo",
    "_db_path",
    "_open",
    "get_task_repo",
]
