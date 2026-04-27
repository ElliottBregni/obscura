"""Tests for obscura.memory.events — event emission on memory writes."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from obscura.auth.models import AuthenticatedUser
from obscura.memory import MemoryKey, MemoryStore
from obscura.memory import events as events_mod
from obscura.memory.events import (
    InProcessSink,
    MemoryEvent,
    NullSink,
    PgNotifySink,
    get_default_sink,
    make_event,
    set_default_sink,
    subscribe,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def test_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="u-events",
        email="events@obscura.dev",
        roles=("admin",),
        org_id="org-1",
        token_type="user",
        raw_token="fake-token",
    )


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "events_memory.db"


@pytest.fixture(autouse=True)
def reset_default_sink() -> Iterator[None]:
    set_default_sink(None)
    yield
    set_default_sink(None)


class _Collector:
    """Capture events into a list with a sync barrier."""

    def __init__(self) -> None:
        self.events: list[MemoryEvent] = []

    def __call__(self, event: MemoryEvent) -> None:
        self.events.append(event)

    def wait_for(self, n: int, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.events) >= n:
                return
            time.sleep(0.01)
        msg = f"expected {n} events, got {len(self.events)}"
        raise AssertionError(msg)


class TestNullSink:
    def test_discards_everything(self) -> None:
        sink = NullSink()
        sink.emit(
            make_event(
                kind="set",
                key=MemoryKey(namespace="n", key="k"),
                value={"x": 1},
                ttl_seconds=None,
                source="kv",
                user_id="u",
            ),
        )


class TestInProcessSink:
    def test_subscriber_receives_emitted_event(self) -> None:
        sink = InProcessSink()
        collector = _Collector()
        sink.subscribe(collector)

        event = make_event(
            kind="set",
            key=MemoryKey(namespace="n", key="k"),
            value=42,
            ttl_seconds=None,
            source="kv",
            user_id="u",
        )
        sink.emit(event)
        collector.wait_for(1)

        assert collector.events[0].key == MemoryKey(namespace="n", key="k")
        assert collector.events[0].value == 42

    def test_unsubscribe_stops_delivery(self) -> None:
        sink = InProcessSink()
        collector = _Collector()
        unsub = sink.subscribe(collector)

        sink.emit(_ev(1))
        collector.wait_for(1)
        unsub()

        sink.emit(_ev(2))
        # Give the drain thread a chance to process the second event
        time.sleep(0.05)
        assert len(collector.events) == 1

    def test_subscriber_exception_does_not_break_others(self) -> None:
        sink = InProcessSink()
        good = _Collector()

        def _bad(_: MemoryEvent) -> None:
            msg = "boom"
            raise RuntimeError(msg)

        sink.subscribe(_bad)
        sink.subscribe(good)

        sink.emit(_ev(1))
        good.wait_for(1)
        assert len(good.events) == 1

    def test_drop_oldest_when_full(self) -> None:
        sink = InProcessSink(max_queue=2)
        # Don't start draining — subscribe a blocking collector
        barrier = {"released": False}

        def _blocker(_: MemoryEvent) -> None:
            while not barrier["released"]:
                time.sleep(0.005)

        sink.subscribe(_blocker)
        # The first event enters the drain thread immediately.
        # The next two fill the queue; a fourth should drop-oldest, not block.
        for i in range(4):
            sink.emit(_ev(i))
        # No assertion on ordering — just that emit didn't block
        barrier["released"] = True


class TestPgEncoding:
    def test_elides_oversized_value(self) -> None:
        big = "x" * 10_000
        event = make_event(
            kind="set",
            key=MemoryKey(namespace="n", key="k"),
            value=big,
            ttl_seconds=None,
            source="vector",
            user_id="u",
        )
        payload = events_mod._encode_for_pg(event)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        body = json.loads(payload)
        assert body["value"] is None
        assert body["value_elided"] is True

    def test_inlines_small_value(self) -> None:
        event = make_event(
            kind="set",
            key=MemoryKey(namespace="n", key="k"),
            value={"small": "ok"},
            ttl_seconds=None,
            source="kv",
            user_id="u",
        )
        payload = events_mod._encode_for_pg(event)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        body = json.loads(payload)
        assert body["value"] == {"small": "ok"}
        assert "value_elided" not in body


class TestPgNotifySinkFallback:
    def test_no_pool_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force is_pg_configured to False — sink must construct cleanly
        # and drop events without raising.
        monkeypatch.setenv("OBSCURA_DB_TYPE", "sqlite")
        sink = PgNotifySink()
        sink.emit(_ev(1))  # must not raise


class TestMemoryStoreEventEmission:
    def test_set_emits_event(
        self,
        test_user: AuthenticatedUser,
        temp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_MEMORY_EVENTS", "local")
        collector = _Collector()
        subscribe(collector)

        store = MemoryStore(test_user, db_path=temp_db)
        store.set("k", {"a": 1}, namespace="ns")

        collector.wait_for(1)
        event = collector.events[0]
        assert event.kind == "set"
        assert event.source == "kv"
        assert event.user_id == test_user.user_id
        assert event.key == MemoryKey(namespace="ns", key="k")
        assert event.value == {"a": 1}

    def test_delete_emits_only_when_row_existed(
        self,
        test_user: AuthenticatedUser,
        temp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_MEMORY_EVENTS", "local")
        collector = _Collector()
        subscribe(collector)

        store = MemoryStore(test_user, db_path=temp_db)
        store.delete("missing", namespace="ns")
        time.sleep(0.05)
        assert collector.events == []

        store.set("present", {"x": 1}, namespace="ns")
        collector.wait_for(1)
        store.delete("present", namespace="ns")
        collector.wait_for(2)

        assert [e.kind for e in collector.events] == ["set", "delete"]

    def test_get_on_expired_emits_expire_not_delete(
        self,
        test_user: AuthenticatedUser,
        temp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_MEMORY_EVENTS", "local")
        collector = _Collector()
        subscribe(collector)

        store = MemoryStore(test_user, db_path=temp_db)
        store.set("exp", {"x": 1}, namespace="ns", ttl=timedelta(milliseconds=10))
        collector.wait_for(1)
        # Wait long enough for TTL to elapse, then read
        time.sleep(0.05)
        result = store.get("exp", namespace="ns")
        assert result is None
        collector.wait_for(2)

        kinds = [e.kind for e in collector.events]
        assert kinds == ["set", "expire"]
        assert "delete" not in kinds

    def test_explicit_event_sink_beats_default(
        self,
        test_user: AuthenticatedUser,
        temp_db: Path,
    ) -> None:
        # Default sink remains NullSink; inject explicit sink per instance.
        sink = InProcessSink()
        collector = _Collector()
        sink.subscribe(collector)

        store = MemoryStore(test_user, db_path=temp_db, event_sink=sink)
        store.set("k", {"x": 1}, namespace="ns")
        collector.wait_for(1)

        assert collector.events[0].kind == "set"


class TestDefaultSinkFactory:
    def test_none_yields_null_sink(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OBSCURA_MEMORY_EVENTS", "none")
        set_default_sink(None)
        assert isinstance(get_default_sink(), NullSink)

    def test_local_yields_inprocess_sink(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_MEMORY_EVENTS", "local")
        set_default_sink(None)
        assert isinstance(get_default_sink(), InProcessSink)

    def test_unknown_mode_falls_back_to_null(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_MEMORY_EVENTS", "garbage")
        set_default_sink(None)
        assert isinstance(get_default_sink(), NullSink)

    def test_subscribe_requires_inprocess_sink(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_MEMORY_EVENTS", "none")
        set_default_sink(None)
        with pytest.raises(RuntimeError, match="InProcessSink"):
            subscribe(lambda _: None)


def _ev(seq: int) -> MemoryEvent:
    return make_event(
        kind="set",
        key=MemoryKey(namespace="n", key=f"k{seq}"),
        value=seq,
        ttl_seconds=None,
        source="kv",
        user_id="u",
    )
