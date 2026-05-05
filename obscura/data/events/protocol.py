"""Domain types and Protocol for the event repository."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from obscura.core.enums.agent import AgentEventKind
from obscura.core.enums.lifecycle import SessionStatus
from obscura.core.models.lifecycle import SessionRecord as SessionRecord
from obscura.core.types import AgentEvent


@dataclass(frozen=True)
class EventRecord:
    """A single persisted event in the append-only log."""

    session_id: str
    seq: int
    kind: AgentEventKind
    payload: dict[str, Any]
    timestamp: datetime


@runtime_checkable
class EventRepo(Protocol):
    """Backend-agnostic event repository.

    Sessions live in their own table; events form an append-only log
    keyed by ``(session_id, seq)``. ``seq`` is monotonically increasing
    per session — collisions raise.

    Implementations: :class:`obscura.data.events.sqlite.SqliteEventRepo`.
    Postgres impl is a Phase 3b task.
    """

    async def create_session(
        self,
        session_id: str,
        agent: str,
        *,
        backend: str = "",
        model: str = "",
        source: str = "live",
        parent_session_id: str = "",
        project: str = "",
        summary: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> SessionRecord: ...

    async def get_session(self, session_id: str) -> SessionRecord | None: ...

    async def update_status(
        self,
        session_id: str,
        status: SessionStatus,
    ) -> None: ...

    async def update_session(
        self,
        session_id: str,
        *,
        summary: str | None = None,
        message_count: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None: ...

    async def append(
        self,
        session_id: str,
        event: AgentEvent,
    ) -> EventRecord: ...

    async def get_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
    ) -> list[EventRecord]: ...

    async def list_sessions(
        self,
        *,
        status: SessionStatus | None = None,
        backend: str | None = None,
        source: str | None = None,
        parent_session_id: str | None = None,
    ) -> list[SessionRecord]: ...

    async def reap_orphaned_sessions(self) -> int: ...

    def close(self) -> None: ...
