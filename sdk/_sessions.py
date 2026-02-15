"""
sdk._sessions — Lightweight session tracking across backends.

Backends handle actual session persistence. This store provides a
unified index so the CLI can list and resume sessions across both
Copilot and Claude without querying each SDK separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, cast

from sdk._types import Backend, SessionRef


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

    _sessions: dict[str, SessionRef] = field(default_factory=lambda: {})

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


class PersistentSessionStore(SessionStore):
    """File-backed session store for agent recovery after interruption.

    Serialises the session index to a JSON file so it survives process
    restarts.  Call :meth:`save` after mutations and :meth:`load` at
    startup.

    Usage::

        store = PersistentSessionStore(Path("~/.obscura/sessions.json"))
        store.load()          # restore from disk
        store.add(ref)
        store.save()          # flush to disk
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path

    def save(self) -> None:
        """Serialise current sessions to disk."""
        import json

        data: list[dict[str, str]] = []
        for ref in self._sessions.values():
            data.append({
                "session_id": ref.session_id,
                "backend": ref.backend.value,
            })

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load(self) -> None:
        """Load sessions from disk, merging with any already in memory."""
        import json

        if not self._path.exists():
            return

        raw = self._path.read_text(encoding="utf-8")
        if not raw.strip():
            return

        for raw_item in json.loads(raw):
            if not isinstance(raw_item, Mapping):
                continue
            item = cast(Mapping[str, object], raw_item)
            session_id_obj = item.get("session_id")
            backend_name_obj = item.get("backend")
            if not isinstance(session_id_obj, str) or not isinstance(backend_name_obj, str):
                continue
            session_id = session_id_obj
            backend_name = backend_name_obj
            if not session_id or not backend_name:
                continue
            ref = SessionRef(
                session_id=session_id,
                backend=Backend(backend_name),
            )
            self._sessions[ref.session_id] = ref
