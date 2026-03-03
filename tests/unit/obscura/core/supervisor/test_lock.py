"""Tests for the SQLite advisory session lock."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from obscura.core.supervisor.errors import LockAcquisitionError
from obscura.core.supervisor.lock import SessionLock


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_lock.db"


@pytest.fixture
def lock(db_path: Path) -> SessionLock:
    lk = SessionLock(db_path, default_ttl=5.0)
    yield lk
    lk.close()


class TestSessionLock:
    """Advisory lock enforces single-writer semantics."""

    @pytest.mark.asyncio
    async def test_acquire_and_release(self, lock: SessionLock) -> None:
        info = await lock.acquire("sess-1", "holder-a", timeout=1.0)
        assert info.session_id == "sess-1"
        assert info.holder_id == "holder-a"
        assert not info.is_expired

        released = await lock.release("sess-1", "holder-a")
        assert released is True

    @pytest.mark.asyncio
    async def test_reentrant_acquisition(self, lock: SessionLock) -> None:
        """Same holder can re-acquire."""
        await lock.acquire("sess-1", "holder-a", timeout=1.0)
        info = await lock.acquire("sess-1", "holder-a", timeout=1.0)
        assert info.holder_id == "holder-a"

    @pytest.mark.asyncio
    async def test_blocked_by_other_holder(self, lock: SessionLock) -> None:
        """Different holder is blocked."""
        await lock.acquire("sess-1", "holder-a", timeout=1.0)
        with pytest.raises(LockAcquisitionError):
            await lock.acquire("sess-1", "holder-b", timeout=0.5)

    @pytest.mark.asyncio
    async def test_release_wrong_holder(self, lock: SessionLock) -> None:
        """Release by wrong holder returns False."""
        await lock.acquire("sess-1", "holder-a", timeout=1.0)
        released = await lock.release("sess-1", "holder-b")
        assert released is False

    @pytest.mark.asyncio
    async def test_expired_lock_stolen(self, db_path: Path) -> None:
        """Expired lock can be stolen by new holder."""
        lock = SessionLock(db_path, default_ttl=0.1)  # 100ms TTL
        await lock.acquire("sess-1", "holder-a", timeout=1.0, ttl=0.1)

        # Wait for expiry
        await asyncio.sleep(0.2)

        # New holder can steal
        info = await lock.acquire("sess-1", "holder-b", timeout=1.0)
        assert info.holder_id == "holder-b"
        lock.close()

    @pytest.mark.asyncio
    async def test_heartbeat_refreshes_ttl(self, lock: SessionLock) -> None:
        await lock.acquire("sess-1", "holder-a", timeout=1.0)
        refreshed = await lock.heartbeat("sess-1", "holder-a")
        assert refreshed is True

    @pytest.mark.asyncio
    async def test_heartbeat_wrong_holder(self, lock: SessionLock) -> None:
        await lock.acquire("sess-1", "holder-a", timeout=1.0)
        refreshed = await lock.heartbeat("sess-1", "holder-b")
        assert refreshed is False

    @pytest.mark.asyncio
    async def test_is_locked(self, lock: SessionLock) -> None:
        assert await lock.is_locked("sess-1") is False
        await lock.acquire("sess-1", "holder-a", timeout=1.0)
        assert await lock.is_locked("sess-1") is True
        await lock.release("sess-1", "holder-a")
        assert await lock.is_locked("sess-1") is False

    @pytest.mark.asyncio
    async def test_cleanup_expired(self, db_path: Path) -> None:
        lock = SessionLock(db_path, default_ttl=0.1)
        await lock.acquire("sess-1", "holder-a", timeout=1.0, ttl=0.1)
        await lock.acquire("sess-2", "holder-b", timeout=1.0, ttl=0.1)
        await asyncio.sleep(0.2)

        cleaned = await lock.cleanup_expired()
        assert cleaned == 2
        lock.close()

    @pytest.mark.asyncio
    async def test_multiple_sessions_independent(self, lock: SessionLock) -> None:
        """Locks on different sessions are independent."""
        await lock.acquire("sess-1", "holder-a", timeout=1.0)
        info2 = await lock.acquire("sess-2", "holder-b", timeout=1.0)
        assert info2.session_id == "sess-2"

    @pytest.mark.asyncio
    async def test_get_lock_info(self, lock: SessionLock) -> None:
        assert await lock.get_lock("sess-1") is None
        await lock.acquire("sess-1", "holder-a", timeout=1.0)
        info = await lock.get_lock("sess-1")
        assert info is not None
        assert info.holder_id == "holder-a"
