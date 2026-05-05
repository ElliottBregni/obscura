"""obscura.data.events — durable event-sourced session persistence.

Phase 3a of the data-layer migration. The implementation moved from
``obscura.core.event_store``; that module is now a thin re-export shim
so existing consumers keep working without import changes.

Public API:

* :class:`EventRecord` — single persisted event in the append-only log
* :class:`SessionRecord` — session metadata (re-exported from
  ``obscura.core.models.lifecycle``)
* :class:`EventRepo` — Protocol every backend implements
* :func:`get_event_repo` — factory; SQLite-only this turn (Postgres
  scaffold raises NotImplementedError until Phase 3b)

Why "events" and not "session_store": the table model is event-sourced
— sessions are recovered by replaying events. Naming follows the
storage shape, not the surface.
"""

from __future__ import annotations

from obscura.data.events.factory import (
    get_event_repo as get_event_repo,
)
from obscura.data.events.protocol import (
    EventRecord as EventRecord,
)
from obscura.data.events.protocol import (
    EventRepo as EventRepo,
)
from obscura.data.events.protocol import (
    SessionRecord as SessionRecord,
)
