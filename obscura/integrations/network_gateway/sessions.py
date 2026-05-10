"""obscura.integrations.network_gateway.sessions — In-memory session store.

Maintains per-session message history for the network gateway WebSocket handler.
Sessions expire after 1 hour of inactivity and are cleaned up lazily.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_SESSION_TTL_SECONDS: float = 3600.0  # 1 hour


class GatewaySessionStore:
    """In-memory session store for the network gateway.

    Thread-safe via ``asyncio.Lock``. Sessions older than
    ``_SESSION_TTL_SECONDS`` of inactivity are reaped lazily on each write.
    """

    def __init__(self) -> None:
        """Initialise an empty session store."""
        self._lock = asyncio.Lock()
        # session_id -> list of {"role": str, "content": str}
        self._history: dict[str, list[dict[str, str]]] = {}
        # session_id -> last-access epoch (monotonic)
        self._last_access: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_history(self, session_id: str) -> list[dict[str, Any]]:
        """Return a copy of the message history for *session_id*.

        Returns an empty list when the session does not exist.
        """
        async with self._lock:
            self._touch(session_id)
            return list(self._history.get(session_id, []))

    async def append(self, session_id: str, role: str, content: str) -> None:
        """Append a ``{"role": role, "content": content}`` entry to *session_id*.

        Creates the session if it does not exist, and reaps expired sessions.
        """
        async with self._lock:
            self._reap_expired()
            if session_id not in self._history:
                self._history[session_id] = []
            self._history[session_id].append({"role": role, "content": content})
            self._touch(session_id)

    async def clear(self, session_id: str) -> None:
        """Delete all history for *session_id*."""
        async with self._lock:
            self._history.pop(session_id, None)
            self._last_access.pop(session_id, None)
            logger.debug("GatewaySessionStore: cleared session %s", session_id)

    async def active_sessions(self) -> int:
        """Return the number of currently live sessions."""
        async with self._lock:
            return len(self._history)

    # ------------------------------------------------------------------
    # Internal helpers  (must be called with _lock held)
    # ------------------------------------------------------------------

    def _touch(self, session_id: str) -> None:
        """Update the last-access timestamp for *session_id*."""
        self._last_access[session_id] = time.monotonic()

    def _reap_expired(self) -> None:
        """Remove sessions that have been idle longer than the TTL."""
        now = time.monotonic()
        expired = [
            sid
            for sid, ts in self._last_access.items()
            if now - ts > _SESSION_TTL_SECONDS
        ]
        for sid in expired:
            self._history.pop(sid, None)
            self._last_access.pop(sid, None)
            logger.debug("GatewaySessionStore: reaped expired session %s", sid)


class _SingletonStore:
    """Lazy-initialised process-wide session store wrapper."""

    _instance: GatewaySessionStore | None = None

    @classmethod
    def get(cls) -> GatewaySessionStore:
        """Return (or create) the singleton :class:`GatewaySessionStore`."""
        if cls._instance is None:
            cls._instance = GatewaySessionStore()
        return cls._instance


def get_session_store() -> GatewaySessionStore:
    """Return the process-wide :class:`GatewaySessionStore` singleton."""
    return _SingletonStore.get()
