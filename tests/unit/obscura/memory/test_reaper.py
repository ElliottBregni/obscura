"""Tests for obscura.memory.reaper — background expire-event emission."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from obscura.auth.models import AuthenticatedUser
from obscura.memory import MemoryStore
from obscura.memory.events import MemoryEvent, set_default_sink, subscribe
from obscura.memory.reaper import ExpirationReaper, start_reaper, stop_reaper

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def test_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="u-reaper",
        email="reaper@obscura.dev",
        roles=("admin",),
        org_id="org-1",
        token_type="user",
        raw_token="fake-token",
    )


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "reaper_memory.db"


@pytest.fixture(autouse=True)
def reset_default_sink() -> Iterator[None]:
    set_default_sink(None)
    yield
    set_default_sink(None)
    stop_reaper()
    MemoryStore.reset_instances()


class _Collector:
    def __init__(self) -> None:
        self.events: list[MemoryEvent] = []

    def __call__(self, event: MemoryEvent) -> None:
        self.events.append(event)

    def wait_for_kinds(
        self,
        kinds: list[str],
        timeout: float = 2.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            observed = [e.kind for e in self.events]
            if observed == kinds:
                return
            time.sleep(0.01)
        msg = f"expected kinds={kinds}, got={[e.kind for e in self.events]}"
        raise AssertionError(msg)


class TestReapExpiredMethod:
    def test_reap_emits_expire_for_each_row(
        self,
        test_user: AuthenticatedUser,
        temp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_MEMORY_EVENTS", "local")
        collector = _Collector()
        subscribe(collector)

        store = MemoryStore(test_user, db_path=temp_db)
        store.set("a", {"x": 1}, namespace="ns", ttl=timedelta(milliseconds=10))
        store.set("b", {"x": 2}, namespace="ns", ttl=timedelta(milliseconds=10))
        store.set("keep", {"x": 3}, namespace="ns")

        # Wait for TTL to elapse
        time.sleep(0.05)

        reaped = store.reap_expired()
        assert reaped == 2

        # Must still have: 2 set events then the two expires (order within
        # a set-batch is preserved; expire ordering is by key scan, not
        # user-observable, so assert on counts + kinds.)
        collector.wait_for_kinds(["set", "set", "set", "expire", "expire"])
        expire_events = [e for e in collector.events if e.kind == "expire"]
        assert {e.key.key for e in expire_events} == {"a", "b"}
        for ev in expire_events:
            assert ev.value is None
            assert ev.ttl_seconds is None
            assert ev.source == "kv"

    def test_reap_is_idempotent(
        self,
        test_user: AuthenticatedUser,
        temp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_MEMORY_EVENTS", "local")
        collector = _Collector()
        subscribe(collector)

        store = MemoryStore(test_user, db_path=temp_db)
        store.set("a", {"x": 1}, namespace="ns", ttl=timedelta(milliseconds=10))
        time.sleep(0.05)

        assert store.reap_expired() == 1
        assert store.reap_expired() == 0  # nothing left to reap

    def test_reap_does_not_double_emit_with_lazy_expire(
        self,
        test_user: AuthenticatedUser,
        temp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Race: lazy-expire runs, then reaper runs. Only one emit."""
        monkeypatch.setenv("OBSCURA_MEMORY_EVENTS", "local")
        collector = _Collector()
        subscribe(collector)

        store = MemoryStore(test_user, db_path=temp_db)
        store.set("a", {"x": 1}, namespace="ns", ttl=timedelta(milliseconds=10))
        time.sleep(0.05)

        # Lazy-expire path: get() sees expired, deletes + emits
        assert store.get("a", namespace="ns") is None
        # Reaper now has nothing to do
        assert store.reap_expired() == 0

        collector.wait_for_kinds(["set", "expire"])


class TestExpirationReaperThread:
    def test_tick_once_reaps_across_live_stores(
        self,
        test_user: AuthenticatedUser,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_MEMORY_EVENTS", "local")
        monkeypatch.setenv("OBSCURA_MEMORY_DIR", str(tmp_path))
        collector = _Collector()
        subscribe(collector)

        # for_user() registers the store in MemoryStore._instances so the
        # reaper picks it up — which is the whole point of this test.
        store = MemoryStore.for_user(test_user)
        store.set("a", 1, namespace="ns", ttl=timedelta(milliseconds=10))
        time.sleep(0.05)

        reaper = ExpirationReaper(interval_seconds=60)  # won't auto-run
        assert reaper.tick_once() == 1

        collector.wait_for_kinds(["set", "expire"])

    def test_start_stop_lifecycle(self) -> None:
        reaper = ExpirationReaper(interval_seconds=60)
        assert not reaper.is_running()
        reaper.start()
        assert reaper.is_running()
        reaper.stop()
        assert not reaper.is_running()

    def test_start_is_idempotent(self) -> None:
        reaper = ExpirationReaper(interval_seconds=60)
        reaper.start()
        assert reaper.is_running()
        reaper.start()  # second call is a no-op
        assert reaper.is_running()
        reaper.stop()

    def test_rejects_nonpositive_interval(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            ExpirationReaper(interval_seconds=0)
        with pytest.raises(ValueError, match="positive"):
            ExpirationReaper(interval_seconds=-1.0)

    def test_per_store_exception_does_not_abort_tick(
        self,
        test_user: AuthenticatedUser,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A broken store's reap_expired failure must not stop other stores."""
        monkeypatch.setenv("OBSCURA_MEMORY_EVENTS", "local")
        monkeypatch.setenv("OBSCURA_MEMORY_DIR", str(tmp_path))
        collector = _Collector()
        subscribe(collector)

        good = MemoryStore.for_user(test_user)
        good.set("a", 1, namespace="ns", ttl=timedelta(milliseconds=10))
        time.sleep(0.05)

        class _Broken:
            user_id = "broken"

            def reap_expired(self) -> int:
                msg = "simulated failure"
                raise RuntimeError(msg)

        # Inject the broken store into the class-level registry — this
        # deliberately touches a private field because the whole point is
        # to simulate a misbehaving peer in the live-stores snapshot.
        MemoryStore._instances["broken"] = _Broken()  # type: ignore[assignment]  # noqa: SLF001

        reaper = ExpirationReaper(interval_seconds=60)
        total = reaper.tick_once()
        assert total == 1  # good store still reaped

        collector.wait_for_kinds(["set", "expire"])


class TestModuleLevelStartStop:
    def test_start_reaper_returns_singleton(self) -> None:
        r1 = start_reaper(interval_seconds=60)
        r2 = start_reaper(interval_seconds=60)
        assert r1 is r2
