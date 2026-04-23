"""Tests for obscura.auth.revocation.

Uses real SQLite in tmp_path so schema + WAL behaviour is exercised.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from obscura.auth.revocation import TokenBlocklist


@pytest.fixture
def blocklist(tmp_path: Path) -> TokenBlocklist:
    return TokenBlocklist(tmp_path / "revocations.db")


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_revoke_then_is_revoked(blocklist: TokenBlocklist) -> None:
    blocklist.revoke("jti-1", expires_at=time.time() + 60)
    assert blocklist.is_revoked("jti-1") is True


def test_unknown_jti_not_revoked(blocklist: TokenBlocklist) -> None:
    assert blocklist.is_revoked("never-added") is False
    assert blocklist.is_revoked("") is False


def test_expired_entry_is_not_revoked(blocklist: TokenBlocklist) -> None:
    blocklist.revoke("jti-stale", expires_at=time.time() - 1)
    assert blocklist.is_revoked("jti-stale") is False


def test_revoke_is_idempotent(blocklist: TokenBlocklist) -> None:
    blocklist.revoke("jti-1", expires_at=time.time() + 60, reason="first")
    blocklist.revoke("jti-1", expires_at=time.time() + 120, reason="second")
    record = blocklist.get("jti-1")
    assert record is not None
    assert record.reason == "second"


def test_empty_jti_rejected(blocklist: TokenBlocklist) -> None:
    with pytest.raises(ValueError):
        blocklist.revoke("", expires_at=time.time() + 60)


# ---------------------------------------------------------------------------
# User-scoped operations
# ---------------------------------------------------------------------------


def test_revoke_user_accepts_list_of_jtis(blocklist: TokenBlocklist) -> None:
    count = blocklist.revoke_user(
        "alice",
        jtis=["j1", "j2", "j3"],
        expires_at=time.time() + 60,
        reason="compromise",
    )
    assert count == 3
    for jti in ("j1", "j2", "j3"):
        assert blocklist.is_revoked(jti) is True


def test_list_for_user_returns_only_active(blocklist: TokenBlocklist) -> None:
    blocklist.revoke(
        "live", user_id="alice", expires_at=time.time() + 60
    )
    blocklist.revoke(
        "stale", user_id="alice", expires_at=time.time() - 10
    )
    blocklist.revoke(
        "other", user_id="bob", expires_at=time.time() + 60
    )
    records = blocklist.list_for_user("alice")
    jtis = {r.jti for r in records}
    assert jtis == {"live"}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_cache_hits_do_not_query_db(blocklist: TokenBlocklist) -> None:
    """A cached lookup should short-circuit DB access.

    Verified indirectly by deleting the DB file out from under the
    blocklist and confirming subsequent is_revoked still returns True
    — the DB is gone but the cache answers first.
    """
    blocklist.revoke("jti-1", expires_at=time.time() + 60)
    # Warm the cache.
    assert blocklist.is_revoked("jti-1") is True
    # Wipe the underlying file via the module's own clear().
    # (Not testing DB resilience, just that the cache short-circuits.)
    # Delete only the row, then confirm the cache still says revoked.
    conn = blocklist._conn()  # type: ignore[reportPrivateUsage]
    conn.execute("DELETE FROM revocations WHERE jti = 'jti-1'")
    conn.commit()
    assert blocklist.is_revoked("jti-1") is True


def test_cache_bounds_at_max_size(tmp_path: Path) -> None:
    bl = TokenBlocklist(tmp_path / "r.db", cache_size=3)
    for i in range(10):
        bl.revoke(f"jti-{i}", expires_at=time.time() + 60)
    # All ten are in the DB.
    for i in range(10):
        assert bl.get(f"jti-{i}") is not None
    # Cache should be bounded to 3 — verify by peeking at the internal
    # structure (explicit testing hook would be cleaner; for now,
    # pyright-disable).
    assert len(bl._cache) <= 3  # type: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


def test_purge_removes_expired(blocklist: TokenBlocklist) -> None:
    blocklist.revoke("live", expires_at=time.time() + 60)
    blocklist.revoke("stale-1", expires_at=time.time() - 10)
    blocklist.revoke("stale-2", expires_at=time.time() - 20)
    removed = blocklist.purge()
    assert removed == 2
    assert blocklist.get("live") is not None
    assert blocklist.get("stale-1") is None


def test_clear_wipes_everything(blocklist: TokenBlocklist) -> None:
    blocklist.revoke("jti-1", expires_at=time.time() + 60)
    blocklist.clear()
    assert blocklist.is_revoked("jti-1") is False
