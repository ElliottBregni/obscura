"""obscura.core.event_store — back-compat shim.

The implementation moved to :mod:`obscura.data.events` as part of the
data-layer migration (Phase 3a). This module re-exports the public
surface under its historical names so the ~14 existing consumers don't
have to flip their imports today. New code should import from
``obscura.data.events`` directly.

Migration plan:
* Phase 3a (this commit) — move + shim, no caller changes
* Phase 3b — Postgres backend + factory env routing
* A later cleanup pass deletes this shim once consumers migrate
"""

from __future__ import annotations

from obscura.core.enums.lifecycle import (
    SESSION_VALID_TRANSITIONS as VALID_TRANSITIONS,
)
from obscura.data.events.factory import get_event_repo as get_event_repo
from obscura.data.events.protocol import (
    EventRecord as EventRecord,
)
from obscura.data.events.protocol import (
    EventRepo as EventStoreProtocol,
)
from obscura.data.events.protocol import (
    SessionRecord as SessionRecord,
)
from obscura.data.events.sqlite import SqliteEventRepo as SQLiteEventStore

__all__ = [
    "VALID_TRANSITIONS",
    "EventRecord",
    "EventStoreProtocol",
    "SQLiteEventStore",
    "SessionRecord",
    "get_event_repo",
]
