"""Token blocklist for server-side session revocation.

SOC2 CC6 expects authenticated sessions to be revocable. Obscura's
pre-this-batch posture was "JWT `exp` is the only way a token stops
working" — a compromised token remained valid until its issued expiry,
with no server-initiated way to cut it off. This module is the fix.

The blocklist records JWT IDs (JTIs) that must be rejected even when
their signature and `exp` claim say they're still good. Entries carry
their own TTL matching the token's `exp` so the table self-prunes.

Design notes:

- **SQLite persistence** — blocklist survives restarts. Without that,
  revocation is a process-local ephemeral concept that a pod restart
  erases, which is not a real control.
- **In-memory LRU cache** — every authenticated request checks the
  blocklist, so DB round-trips would eat the auth hot path. The cache
  is bounded (default 10k entries) and populated on miss; correctness
  depends on the cache being consulted *before* the DB so a revocation
  write must invalidate the cache entry.
- **No hard delete** — expired entries are purged, but the revocation
  event is recorded separately via the audit log so the history is
  preserved for CC2. (Emitted by the admin CLI, not this module, to
  keep it dependency-light.)
- **Forward-compatible with the Supabase JWT path.** JTIs are opaque;
  they come from whatever token layer produced them (Supabase JWT,
  API-key-derived, or a future issuance). The blocklist doesn't care
  how the JTI was produced.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_DEFAULT_CACHE_SIZE: Final[int] = 10_000


@dataclass(frozen=True)
class RevocationRecord:
    """One blocklist entry."""

    jti: str
    user_id: str
    revoked_at: float  # epoch seconds
    expires_at: float  # epoch seconds — when the token would have expired
    reason: str  # operator-supplied rationale for the audit trail


class TokenBlocklist:
    """Persistent blocklist of revoked JWT IDs, with a bounded LRU cache.

    The blocklist is process-safe (per-thread SQLite connection) and
    cheap to check on the hot path thanks to the cache. A miss on the
    positive case (token not revoked) still hits the DB — the alternative
    is a false-positive cache that fails open, which is the wrong
    failure mode for a security control.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        cache_size: int = _DEFAULT_CACHE_SIZE,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._cache_size = cache_size
        # Positive cache: jti → expires_at. Bounded LRU so a long-running
        # server doesn't grow unbounded. Misses always consult the DB.
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._init_schema()

    # -- connection management ----------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self) -> None:
        conn = self._conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS revocations (
                jti         TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL DEFAULT '',
                revoked_at  REAL NOT NULL,
                expires_at  REAL NOT NULL,
                reason      TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_revocations_user
                ON revocations(user_id);
            CREATE INDEX IF NOT EXISTS idx_revocations_expires
                ON revocations(expires_at);
            """,
        )
        conn.commit()

    # -- writes --------------------------------------------------------------

    def revoke(
        self,
        jti: str,
        *,
        user_id: str = "",
        expires_at: float,
        reason: str = "",
    ) -> RevocationRecord:
        """Mark ``jti`` as revoked until ``expires_at``.

        Idempotent — revoking the same jti twice is not an error; the
        second call wins (later revoked_at, same expires_at unless the
        caller changed it).
        """
        if not jti:
            raise ValueError("jti must be non-empty")
        record = RevocationRecord(
            jti=jti,
            user_id=user_id,
            revoked_at=time.time(),
            expires_at=expires_at,
            reason=reason,
        )
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO revocations "
            "(jti, user_id, revoked_at, expires_at, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                record.jti,
                record.user_id,
                record.revoked_at,
                record.expires_at,
                record.reason,
            ),
        )
        conn.commit()
        with self._cache_lock:
            self._cache[jti] = expires_at
            self._cache.move_to_end(jti)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return record

    def revoke_user(
        self,
        user_id: str,
        *,
        jtis: list[str],
        expires_at: float,
        reason: str = "",
    ) -> int:
        """Revoke a set of known tokens for a user.

        Obscura does not have a forward-index from user_id → all active
        JTIs today, so the caller supplies the list. When the session
        surface grows one, this method gains a lookup. Returns the count
        of entries written.
        """
        count = 0
        for jti in jtis:
            self.revoke(
                jti,
                user_id=user_id,
                expires_at=expires_at,
                reason=reason,
            )
            count += 1
        return count

    # -- reads ---------------------------------------------------------------

    def is_revoked(self, jti: str, *, now: float | None = None) -> bool:
        """True iff ``jti`` is in the blocklist and not yet expired.

        Misses do NOT populate the cache — otherwise an attacker spraying
        random JTIs could evict real entries. Hits are cached; cache
        entries past their expires_at are treated as misses and evicted.
        """
        if not jti:
            return False
        ts = now if now is not None else time.time()

        with self._cache_lock:
            cached = self._cache.get(jti)
            if cached is not None:
                if cached > ts:
                    self._cache.move_to_end(jti)
                    return True
                # Expired cache entry — drop and treat as miss.
                self._cache.pop(jti, None)

        row = (
            self._conn()
            .execute(
                "SELECT expires_at FROM revocations WHERE jti = ?",
                (jti,),
            )
            .fetchone()
        )
        if row is None:
            return False
        expires_at = float(row[0])
        if expires_at <= ts:
            # Entry past its natural expiry; don't populate the cache,
            # let purge() clean it up eventually.
            return False
        with self._cache_lock:
            self._cache[jti] = expires_at
            self._cache.move_to_end(jti)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return True

    def get(self, jti: str) -> RevocationRecord | None:
        """Retrieve the revocation record, if any (for audit / debugging)."""
        row = (
            self._conn()
            .execute(
                "SELECT jti, user_id, revoked_at, expires_at, reason "
                "FROM revocations WHERE jti = ?",
                (jti,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return RevocationRecord(
            jti=str(row[0]),
            user_id=str(row[1]),
            revoked_at=float(row[2]),
            expires_at=float(row[3]),
            reason=str(row[4]),
        )

    def list_for_user(self, user_id: str) -> list[RevocationRecord]:
        """All non-expired revocations for ``user_id``."""
        rows = (
            self._conn()
            .execute(
                "SELECT jti, user_id, revoked_at, expires_at, reason "
                "FROM revocations WHERE user_id = ? AND expires_at > ?",
                (user_id, time.time()),
            )
            .fetchall()
        )
        return [
            RevocationRecord(
                jti=str(r[0]),
                user_id=str(r[1]),
                revoked_at=float(r[2]),
                expires_at=float(r[3]),
                reason=str(r[4]),
            )
            for r in rows
        ]

    # -- maintenance ---------------------------------------------------------

    def purge(self, *, now: float | None = None) -> int:
        """Remove revocations whose TTL has passed. Returns rows removed.

        Called periodically by a cleanup worker. Safe to call on the hot
        path but usually not worth the contention — prefer a scheduled
        task.
        """
        ts = now if now is not None else time.time()
        conn = self._conn()
        cur = conn.execute(
            "DELETE FROM revocations WHERE expires_at <= ?",
            (ts,),
        )
        conn.commit()
        removed = cur.rowcount
        with self._cache_lock:
            # Drop expired entries from the cache too.
            expired = [k for k, v in self._cache.items() if v <= ts]
            for k in expired:
                self._cache.pop(k, None)
        return removed

    def clear(self) -> None:
        """Testing hook — wipe the blocklist."""
        self._conn().execute("DELETE FROM revocations")
        self._conn().commit()
        with self._cache_lock:
            self._cache.clear()


# ---------------------------------------------------------------------------
# Singleton accessor so the middleware can reach a blocklist without
# threading state through every caller.
# ---------------------------------------------------------------------------

_default_blocklist: TokenBlocklist | None = None
_default_lock = threading.Lock()


def default_blocklist() -> TokenBlocklist:
    """Process-wide blocklist rooted at ``~/.obscura/revocations.db``."""
    global _default_blocklist
    if _default_blocklist is not None:
        return _default_blocklist
    with _default_lock:
        if _default_blocklist is None:
            import os

            path = os.environ.get(
                "OBSCURA_REVOCATIONS_DB",
                str(Path.home() / ".obscura" / "revocations.db"),
            )
            _default_blocklist = TokenBlocklist(path)
    return _default_blocklist


def reset_default_blocklist() -> None:
    """Testing hook — clears the module-level singleton."""
    global _default_blocklist
    _default_blocklist = None
