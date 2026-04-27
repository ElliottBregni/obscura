"""obscura.internal.sessions — Lightweight session tracking across backends.

``SessionStore`` provides in-memory runtime tracking for active sessions.
Backends that need to map session IDs to SDK objects (threads, etc.) use
this store.  Persistent session metadata lives in
``obscura.core.event_store.SQLiteEventStore``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from obscura.core.types import Backend, SessionRef

# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------


@dataclass
class SessionStore:
    """In-memory session index.

    Tracks active/known sessions. Delegates actual persistence to the
    underlying backend (Copilot has native session storage, Claude uses
    file-based checkpoints).
    """

    _sessions: dict[str, SessionRef] = field(
        default_factory=lambda: cast("dict[str, SessionRef]", {}),
    )

    def add(self, ref: SessionRef) -> None:
        """Register a session reference."""
        self._sessions[ref.session_id] = ref

    def get(self, session_id: str) -> SessionRef | None:
        """Look up a session by ID."""
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> None:
        """Remove a session from the index."""
        self._sessions.pop(session_id, None)

    def list_all(self, backend: Backend | None = None) -> list[SessionRef]:
        """List all tracked sessions, optionally filtered by backend."""
        refs = list(self._sessions.values())
        if backend is not None:
            refs = [r for r in refs if r.backend == backend]
        return refs

    def __len__(self) -> int:
        return len(self._sessions)

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._sessions
