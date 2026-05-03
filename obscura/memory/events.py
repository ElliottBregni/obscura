"""obscura.memory.events — Event emission for memory writes.

Writes to ``MemoryStore`` and ``VectorMemoryStore`` fan out a :class:`MemoryEvent`
through an :class:`EventSink` after the backing store has durably committed.
Default sink is :class:`NullSink` (zero overhead); swap via ``OBSCURA_MEMORY_EVENTS``.

Sinks:

- ``none``  → :class:`NullSink` — drops everything, default.
- ``local`` → :class:`InProcessSink` — fan out to in-process subscribers on a
  single drain thread. Writes never block: bounded queue, drop-oldest.
- ``pg``    → :class:`PgNotifySink` — ``pg_notify()`` per event over the shared
  PG pool. Payload is elided above ~7 KB; subscribers refetch by key.

Events are emitted **after** the underlying commit so subscribers never see
phantom writes. In-process ordering is preserved by the single drain thread;
cross-process ordering via PG is preserved because ``pg_notify`` is
transaction-scoped and a single connection serializes notifications.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from queue import Empty, Full, Queue
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from obscura.memory import MemoryKey

if TYPE_CHECKING:
    from collections.abc import Callable

_log = logging.getLogger(__name__)

# Postgres NOTIFY payload ceiling is 8000 bytes; leave headroom for framing.
_PG_PAYLOAD_SOFT_LIMIT = 7000

EventKind = Literal["set", "delete", "expire"]
EventSource = Literal["kv", "vector"]


@dataclass(frozen=True)
class MemoryEvent:
    """A single memory mutation.

    ``value`` is whatever the store persisted — a JSON-serializable object for
    KV writes, or the text body for vector writes. ``None`` for ``delete`` /
    ``expire``. ``event_id`` is per-process monotonic (good for local
    ordering); ``event_uuid`` is globally unique (use for cross-process
    dedupe).
    """

    kind: EventKind
    key: MemoryKey
    value: Any | None
    ttl_seconds: float | None
    source: EventSource
    user_id: str
    at: datetime
    event_id: int
    event_uuid: str


@runtime_checkable
class EventSink(Protocol):
    """Anything that accepts ``MemoryEvent`` instances."""

    def emit(self, event: MemoryEvent) -> None: ...


class NullSink:
    """Discards events. Default — zero overhead on the hot path."""

    def emit(self, event: MemoryEvent) -> None:  # noqa: ARG002
        return


class InProcessSink:
    """Fan out to in-process subscribers.

    Writes enqueue and return immediately. A single daemon thread drains the
    queue and calls each subscriber; exceptions are swallowed so one bad
    subscriber can't break the chain. When the queue is full, the oldest
    event is dropped so writers never block.
    """

    def __init__(self, max_queue: int = 10_000) -> None:
        self._queue: Queue[MemoryEvent] = Queue(maxsize=max_queue)
        self._subscribers: list[Callable[[MemoryEvent], None]] = []
        self._subs_lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._drain,
            name="obscura-memory-events",
            daemon=True,
        )
        self._worker.start()

    def emit(self, event: MemoryEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except Full:
            # drop-oldest: preserve recency, never block the writer
            _log.debug("suppressed exception in emit", exc_info=True)
            with contextlib.suppress(Empty):
                self._queue.get_nowait()
            with contextlib.suppress(Full):
                self._queue.put_nowait(event)

    def subscribe(self, fn: Callable[[MemoryEvent], None]) -> Callable[[], None]:
        """Register a subscriber. Returns an unsubscribe callable."""
        with self._subs_lock:
            self._subscribers.append(fn)

        def _unsubscribe() -> None:
            with self._subs_lock:
                with contextlib.suppress(ValueError):
                    self._subscribers.remove(fn)

        return _unsubscribe

    def _drain(self) -> None:
        while True:
            event = self._queue.get()
            with self._subs_lock:
                subs = list(self._subscribers)
            for sub in subs:
                try:
                    sub(event)
                except Exception:
                    _log.exception("memory event subscriber raised")


class PgNotifySink:
    """Publish events via Postgres ``pg_notify``.

    Requires the shared PG pool to be configured (``OBSCURA_DB_TYPE=postgresql``).
    If the pool isn't available at construction time, falls through to a null
    behaviour and logs once — we prefer silent writes over crashing the store.
    """

    def __init__(self, channel: str = "obscura_memory") -> None:
        self._channel = channel
        self._pool: Any | None = None
        try:
            from obscura.core.pg_config import PGPoolManager, is_pg_configured

            if is_pg_configured():
                self._pool = PGPoolManager.get_pool()
            else:
                _log.warning(
                    "PgNotifySink configured but OBSCURA_DB_TYPE != postgresql; "
                    "events will be dropped",
                )
        except Exception:
            _log.exception("PgNotifySink: failed to acquire PG pool")

    def emit(self, event: MemoryEvent) -> None:
        if self._pool is None:
            return
        payload = _encode_for_pg(event)
        conn = None
        try:
            conn = self._pool.getconn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_notify(%s, %s)",
                    (self._channel, payload),
                )
            conn.commit()
        except Exception:
            _log.exception("PgNotifySink: pg_notify failed")
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.rollback()
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    self._pool.putconn(conn)


def _encode_for_pg(event: MemoryEvent) -> str:
    """Serialize an event for ``pg_notify``. Elides oversized values."""
    body = _event_to_dict(event)
    payload = json.dumps(body, default=str)
    if len(payload.encode("utf-8")) <= _PG_PAYLOAD_SOFT_LIMIT:
        return payload
    # Value too big for NOTIFY — subscribers must refetch by key.
    body["value"] = None
    body["value_elided"] = True
    return json.dumps(body, default=str)


def _event_to_dict(event: MemoryEvent) -> dict[str, Any]:
    d = asdict(event)
    d["at"] = event.at.isoformat()
    return d


# ---------------------------------------------------------------------------
# Default sink — process-wide singleton, selected via env var.
# ---------------------------------------------------------------------------


_default_sink: EventSink | None = None
_default_sink_lock = threading.Lock()
_event_counter = itertools.count(1)


def _build_default_sink() -> EventSink:
    mode = os.environ.get("OBSCURA_MEMORY_EVENTS", "none").strip().lower()
    if mode == "local":
        return InProcessSink()
    if mode == "pg":
        return PgNotifySink()
    if mode not in ("", "none", "null", "off"):
        _log.warning("Unknown OBSCURA_MEMORY_EVENTS=%r; using NullSink", mode)
    return NullSink()


def get_default_sink() -> EventSink:
    """Return (and lazily construct) the process-wide default sink."""
    global _default_sink
    if _default_sink is None:
        with _default_sink_lock:
            if _default_sink is None:
                _default_sink = _build_default_sink()
    return _default_sink


def set_default_sink(sink: EventSink | None) -> None:
    """Override the default sink. Pass ``None`` to reset (rebuilds from env).

    Intended for tests and for apps that want to inject their own sink at
    startup. Not thread-safe against concurrent readers.
    """
    global _default_sink
    with _default_sink_lock:
        _default_sink = sink


def subscribe(
    fn: Callable[[MemoryEvent], None],
) -> Callable[[], None]:
    """Subscribe to memory events on the default :class:`InProcessSink`.

    Raises ``RuntimeError`` if the default sink isn't an ``InProcessSink`` —
    other transports (``pg``) expose their own subscription mechanism.
    """
    sink = get_default_sink()
    if not isinstance(sink, InProcessSink):
        msg = (
            f"subscribe() requires InProcessSink; default sink is "
            f"{type(sink).__name__}. Set OBSCURA_MEMORY_EVENTS=local."
        )
        raise RuntimeError(msg)
    return sink.subscribe(fn)


def next_event_id() -> int:
    """Return the next process-monotonic event id."""
    return next(_event_counter)


def make_event(
    *,
    kind: EventKind,
    key: MemoryKey,
    value: Any | None,
    ttl_seconds: float | None,
    source: EventSource,
    user_id: str,
) -> MemoryEvent:
    """Build a :class:`MemoryEvent` with timestamps and ids populated."""
    return MemoryEvent(
        kind=kind,
        key=key,
        value=value,
        ttl_seconds=ttl_seconds,
        source=source,
        user_id=user_id,
        at=datetime.now(UTC),
        event_id=next_event_id(),
        event_uuid=uuid.uuid4().hex,
    )


def event_from_pg_payload(payload: str) -> MemoryEvent:
    """Parse a payload emitted by :class:`PgNotifySink` back into a MemoryEvent.

    The ``value_elided`` flag — present when the original value was too big to
    fit in ``pg_notify`` — is dropped here. Callers that care should check
    ``event.value is None`` plus the kind (``set`` with ``value=None`` means
    elided; ``delete``/``expire`` are always ``None`` by design).
    """
    body = json.loads(payload)
    body.pop("value_elided", None)
    key_data = body.pop("key")
    key = MemoryKey(namespace=key_data["namespace"], key=key_data["key"])
    at = datetime.fromisoformat(body.pop("at"))
    return MemoryEvent(key=key, at=at, **body)
