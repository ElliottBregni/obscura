"""obscura.auth.copilot_403_cache -- Remember Copilot's "no" for OAuth tokens.

When the "easy path" Supabase-forwarded GitHub token is handed to Copilot
and Copilot rejects it (403 from ``api.github.com/copilot_internal/v2/token``
because the Supabase OAuth app isn't in GitHub's Copilot allowlist), cache
that fact briefly. Subsequent requests from the same user skip the doomed
OAuth attempt and fall straight through to env/CLI-sourced tokens.

Cache is in-memory, process-local, with a short TTL so a user who resolves
the underlying problem (e.g. re-authorizing with broader scopes) doesn't
have to wait long.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

DEFAULT_TTL_SECONDS = 300  # 5 minutes — balance between avoiding retry loops
# and letting fixes land without restarting the server.


@dataclass(frozen=True)
class _Entry:
    expires_at: float


class Copilot403Cache:
    """Thread-safe TTL cache keyed by (user_id, token_hash-or-prefix)."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def _key(self, user_id: str, token: str) -> str:
        # Include first 8 chars of token so rotation (new token after
        # re-auth) invalidates the cache without bumping user_id. Avoids
        # storing the full secret.
        prefix = (token or "")[:8]
        return f"{user_id}::{prefix}"

    def is_blocked(self, user_id: str, token: str) -> bool:
        """Return True when this user+token was recently rejected by Copilot."""
        if not user_id or not token:
            return False
        key = self._key(user_id, token)
        now = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False
            if entry.expires_at <= now:
                del self._entries[key]
                return False
            return True

    def mark_blocked(self, user_id: str, token: str) -> None:
        """Record that Copilot rejected this user's OAuth token."""
        if not user_id or not token:
            return
        key = self._key(user_id, token)
        with self._lock:
            self._entries[key] = _Entry(expires_at=time.time() + self._ttl)

    def clear(self) -> None:
        """Drop all cached entries (test + admin use)."""
        with self._lock:
            self._entries.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._entries)


# Process-wide singleton. Callers that want custom TTLs can instantiate
# directly.
_cache = Copilot403Cache()


def is_oauth_token_blocked(user_id: str, token: str) -> bool:
    """Public helper: has Copilot recently rejected this user's OAuth token?"""
    return _cache.is_blocked(user_id, token)


def mark_oauth_token_blocked(user_id: str, token: str) -> None:
    """Public helper: record a Copilot 403 for this user's OAuth token."""
    _cache.mark_blocked(user_id, token)


def clear_cache_for_tests() -> None:
    """Test helper — drop all cached 403s."""
    _cache.clear()


__all__ = [
    "Copilot403Cache",
    "clear_cache_for_tests",
    "is_oauth_token_blocked",
    "mark_oauth_token_blocked",
]
