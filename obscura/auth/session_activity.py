"""Idle-timeout tracking for authenticated sessions.

Complements the token blocklist. Revocation is operator-initiated;
idle-timeout is automatic — a session that hasn't been active for
longer than the configured window is refused, even with an otherwise
valid token.

Why in-process + memory-only (no DB):

- Idle-timeout is a per-session hot-path concern. Every request touches
  it. A persistent store on this path adds latency every request, and
  the cost of losing last-activity state at a restart is small — the
  worst case is a legitimate session being treated as fresh for one
  extra window, not a security failure.
- For multi-replica deployments, upgrading to Redis behind this
  interface is a single-method swap; documented below.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Final

_DEFAULT_IDLE_MAX_SECONDS: Final[float] = 60 * 60  # 1 hour
_PRUNE_INTERVAL_SECONDS: Final[float] = 60 * 5  # prune on access every 5 min


@dataclass(frozen=True)
class ActivitySample:
    """One in-memory activity record."""

    session_id: str
    last_seen: float
    first_seen: float


class IdleTimeoutTracker:
    """Tracks last-seen timestamps per session_id.

    Callers invoke :meth:`observe` on every authenticated request and
    :meth:`is_idle` when deciding whether to reject. Thread-safe by a
    single lock — the hot path is short enough that lock contention
    isn't a concern below O(10k) req/s per process. Scale past that by
    sharding the tracker or moving to Redis.
    """

    def __init__(
        self,
        *,
        idle_max_seconds: float | None = None,
    ) -> None:
        self._idle_max_seconds = (
            idle_max_seconds
            if idle_max_seconds is not None
            else _resolve_idle_max_from_env()
        )
        self._activity: dict[str, ActivitySample] = {}
        self._lock = threading.Lock()
        self._last_prune: float = 0.0

    @property
    def idle_max_seconds(self) -> float:
        return self._idle_max_seconds

    def observe(self, session_id: str, *, now: float | None = None) -> None:
        """Record that ``session_id`` is active at ``now`` (defaults to wall time)."""
        if not session_id:
            return
        ts = now if now is not None else time.time()
        with self._lock:
            prior = self._activity.get(session_id)
            if prior is None:
                self._activity[session_id] = ActivitySample(
                    session_id=session_id,
                    first_seen=ts,
                    last_seen=ts,
                )
            else:
                self._activity[session_id] = ActivitySample(
                    session_id=session_id,
                    first_seen=prior.first_seen,
                    last_seen=ts,
                )
            self._maybe_prune_locked(ts)

    def is_idle(self, session_id: str, *, now: float | None = None) -> bool:
        """True iff we have seen this session before AND it's past the idle window.

        An unknown session returns False — "idle" means "was active,
        now isn't." Applying it to sessions we've never observed would
        reject every first request, which is the wrong posture after a
        restart or a cold cache. Callers that want "re-auth on every
        restart" enforce that with a shorter JWT `exp`, not here.
        """
        ts = now if now is not None else time.time()
        with self._lock:
            sample = self._activity.get(session_id)
            if sample is None:
                return False
            return (ts - sample.last_seen) > self._idle_max_seconds

    def forget(self, session_id: str) -> None:
        """Drop the record (e.g., on logout or explicit revocation)."""
        with self._lock:
            self._activity.pop(session_id, None)

    def clear(self) -> None:
        """Testing hook."""
        with self._lock:
            self._activity.clear()
            self._last_prune = 0.0

    def size(self) -> int:
        with self._lock:
            return len(self._activity)

    # ------------------------------------------------------------------

    def _maybe_prune_locked(self, now: float) -> None:
        if (now - self._last_prune) < _PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        # Drop any record older than the idle window — the session is
        # already dead and never coming back without re-auth.
        cutoff = now - self._idle_max_seconds
        dead = [
            sid for sid, s in self._activity.items() if s.last_seen < cutoff
        ]
        for sid in dead:
            self._activity.pop(sid, None)


def _resolve_idle_max_from_env() -> float:
    raw = os.environ.get("OBSCURA_SESSION_IDLE_MAX", "").strip()
    if not raw:
        return _DEFAULT_IDLE_MAX_SECONDS
    try:
        parsed = float(raw)
    except ValueError:
        return _DEFAULT_IDLE_MAX_SECONDS
    if parsed <= 0:
        return _DEFAULT_IDLE_MAX_SECONDS
    return parsed


# ---------------------------------------------------------------------------
# Singleton for middleware convenience
# ---------------------------------------------------------------------------

_default_tracker: IdleTimeoutTracker | None = None
_default_lock = threading.Lock()


def default_tracker() -> IdleTimeoutTracker:
    global _default_tracker
    if _default_tracker is not None:
        return _default_tracker
    with _default_lock:
        if _default_tracker is None:
            _default_tracker = IdleTimeoutTracker()
    return _default_tracker


def reset_default_tracker() -> None:
    """Testing hook."""
    global _default_tracker
    _default_tracker = None
