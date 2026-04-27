"""Tests for obscura.memory.pg_listener — payload parsing and refetch.

No live PostgreSQL is required; the LISTEN loop itself isn't exercised.
We test the parse + dispatch path via ``PgEventListener.handle_payload``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

from obscura.auth.models import AuthenticatedUser
from obscura.memory import MemoryKey, MemoryStore
from obscura.memory import events as events_mod
from obscura.memory.events import (
    MemoryEvent,
    PgNotifySink,
    event_from_pg_payload,
    make_event,
    set_default_sink,
)
from obscura.memory.pg_listener import PgEventListener

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def test_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="u-pglistener",
        email="pg@obscura.dev",
        roles=("admin",),
        org_id="org-1",
        token_type="user",
        raw_token="fake-token",
    )


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "pg_listener_memory.db"


@pytest.fixture(autouse=True)
def reset_default_sink() -> Iterator[None]:
    set_default_sink(None)
    yield
    set_default_sink(None)
    MemoryStore.reset_instances()


class TestEventFromPgPayload:
    def test_round_trip_preserves_core_fields(self) -> None:
        original = make_event(
            kind="set",
            key=MemoryKey(namespace="ns", key="k"),
            value={"a": 1, "b": [2, 3]},
            ttl_seconds=60.0,
            source="kv",
            user_id="u",
        )
        payload = events_mod._encode_for_pg(original)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        parsed = event_from_pg_payload(payload)

        assert parsed.kind == original.kind
        assert parsed.key == original.key
        assert parsed.value == original.value
        assert parsed.ttl_seconds == original.ttl_seconds
        assert parsed.source == original.source
        assert parsed.user_id == original.user_id
        assert parsed.event_id == original.event_id
        assert parsed.event_uuid == original.event_uuid

    def test_elided_payload_parses_with_value_none(self) -> None:
        event = make_event(
            kind="set",
            key=MemoryKey(namespace="ns", key="k"),
            value="x" * 10_000,  # forces elision
            ttl_seconds=None,
            source="kv",
            user_id="u",
        )
        payload = events_mod._encode_for_pg(event)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert json.loads(payload)["value_elided"] is True
        parsed = event_from_pg_payload(payload)
        assert parsed.value is None
        # And the dropped flag must not leak through as a field
        assert not hasattr(parsed, "value_elided")


class TestHandlePayload:
    def test_invokes_subscriber(self) -> None:
        received: list[MemoryEvent] = []
        listener = PgEventListener(on_event=received.append)

        original = make_event(
            kind="set",
            key=MemoryKey(namespace="ns", key="k"),
            value={"a": 1},
            ttl_seconds=None,
            source="kv",
            user_id="u",
        )
        listener.handle_payload(events_mod._encode_for_pg(original))  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        assert len(received) == 1
        assert received[0].key == original.key

    def test_malformed_payload_is_logged_not_raised(self) -> None:
        received: list[MemoryEvent] = []
        listener = PgEventListener(on_event=received.append)
        listener.handle_payload("not valid json")
        assert received == []

    def test_subscriber_exception_is_caught(self) -> None:
        def _bad(_: MemoryEvent) -> None:
            msg = "boom"
            raise RuntimeError(msg)

        listener = PgEventListener(on_event=_bad)
        event = make_event(
            kind="set",
            key=MemoryKey(namespace="ns", key="k"),
            value=None,
            ttl_seconds=None,
            source="kv",
            user_id="u",
        )
        # Must not raise
        listener.handle_payload(events_mod._encode_for_pg(event))  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]


class TestRefetchElided:
    def test_refetches_kv_value_when_elided(
        self,
        test_user: AuthenticatedUser,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # for_user() registers the store so the listener's refetch path
        # (which calls MemoryStore.for_user) finds it.
        monkeypatch.setenv("OBSCURA_MEMORY_DIR", str(tmp_path))
        store = MemoryStore.for_user(test_user)
        real_value = {"big": "x" * 10_000}
        store.set("k", real_value, namespace="ns")

        # Fake an elided event for that key
        event = make_event(
            kind="set",
            key=MemoryKey(namespace="ns", key="k"),
            value=real_value,
            ttl_seconds=None,
            source="kv",
            user_id=test_user.user_id,
        )
        payload = events_mod._encode_for_pg(event)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert json.loads(payload)["value_elided"] is True  # sanity

        received: list[MemoryEvent] = []
        listener = PgEventListener(
            on_event=received.append,
            refetch_elided=True,
            user=test_user,
        )
        listener.handle_payload(payload)

        assert len(received) == 1
        assert received[0].value == real_value

    def test_refetch_requires_user(self) -> None:
        with pytest.raises(ValueError, match="requires a user"):
            PgEventListener(on_event=lambda _: None, refetch_elided=True)

    def test_vector_events_are_not_refetched(
        self,
        test_user: AuthenticatedUser,
    ) -> None:
        """Vector payloads need embeddings to reconstruct — leave elided."""
        received: list[MemoryEvent] = []
        listener = PgEventListener(
            on_event=received.append,
            refetch_elided=True,
            user=test_user,
        )

        event = make_event(
            kind="set",
            key=MemoryKey(namespace="ns", key="k"),
            value="x" * 10_000,
            ttl_seconds=None,
            source="vector",
            user_id=test_user.user_id,
        )
        listener.handle_payload(events_mod._encode_for_pg(event))  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        assert len(received) == 1
        assert received[0].value is None


class TestPgNotifySinkFallback:
    def test_returns_without_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OBSCURA_DB_TYPE", "sqlite")
        sink = PgNotifySink()
        # Construct + emit must not raise
        sink.emit(
            make_event(
                kind="set",
                key=MemoryKey(namespace="n", key="k"),
                value=None,
                ttl_seconds=None,
                source="kv",
                user_id="u",
            ),
        )


class TestUuidField:
    def test_each_event_gets_unique_uuid(self) -> None:
        e1 = make_event(
            kind="set",
            key=MemoryKey(namespace="n", key="a"),
            value=None,
            ttl_seconds=None,
            source="kv",
            user_id="u",
        )
        e2 = make_event(
            kind="set",
            key=MemoryKey(namespace="n", key="a"),
            value=None,
            ttl_seconds=None,
            source="kv",
            user_id="u",
        )
        assert e1.event_uuid != e2.event_uuid
        # 32 hex chars for uuid4().hex
        assert len(e1.event_uuid) == 32
