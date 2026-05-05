"""obscura.memory.pg_listener — Subscribe to PgNotifySink via LISTEN/NOTIFY.

Consumes events produced by :class:`obscura.memory.events.PgNotifySink`.
Use this in a separate process that wants to react to memory writes made by
another process (e.g. a web worker, a background agent, a sync job).

Usage::

    from obscura.memory.pg_listener import PgEventListener

    def on_event(event):
        print(event.kind, event.key, event.value)

    listener = PgEventListener(on_event=on_event)
    listener.start()
    # ... do other work ...
    listener.stop()

Two behaviours worth knowing:

- **Elided values.** When a payload exceeds ~7 KB, ``PgNotifySink`` drops the
  value before sending. Set ``refetch_elided=True`` and pass ``user`` to
  automatically re-read the full value from :class:`MemoryStore` when that
  happens (KV only — vector needs embeddings).
- **Reconnects.** The listen loop treats disconnect as a retryable error and
  reconnects with exponential backoff. Events that fired while disconnected
  are lost (``NOTIFY`` has no persistence); use ``event_uuid`` for dedupe if
  you bolt on a replay log later.
"""

from __future__ import annotations

import logging
import select
import threading
import time
from typing import TYPE_CHECKING

from obscura.memory.events import MemoryEvent, event_from_pg_payload

if TYPE_CHECKING:
    from collections.abc import Callable

    from obscura.auth.models import AuthenticatedUser

_log = logging.getLogger(__name__)

_RECONNECT_INITIAL_SECONDS = 0.5
_RECONNECT_MAX_SECONDS = 30.0


class PgEventListener:
    """Background LISTEN loop for :class:`PgNotifySink` events.

    Spawns a daemon thread that opens its own psycopg2 connection (outside the
    shared pool, because ``LISTEN`` is long-lived) and runs ``select`` on the
    socket. On each notify, decodes the payload and invokes ``on_event``.

    ``on_event`` runs on the listener thread — keep it fast or hand off to
    a queue. Exceptions are caught and logged so one bad handler can't stall
    the listener.
    """

    def __init__(
        self,
        *,
        on_event: Callable[[MemoryEvent], None],
        channel: str = "obscura_memory",
        refetch_elided: bool = False,
        user: AuthenticatedUser | None = None,
    ) -> None:
        if refetch_elided and user is None:
            msg = "refetch_elided=True requires a user to read from MemoryStore"
            raise ValueError(msg)
        self._on_event = on_event
        self._channel = channel
        self._refetch_elided = refetch_elided
        self._user = user
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the listener thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"obscura-pg-listener-{self._channel}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the listener to stop and wait briefly for it to finish."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_running(self) -> bool:
        """Return True if the listener thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def handle_payload(self, raw: str) -> None:
        """Parse a raw NOTIFY payload and fire the subscriber.

        Exposed for tests and for users who want to replay events from a log
        without a live PG connection.
        """
        try:
            event = event_from_pg_payload(raw)
        except Exception:
            _log.exception("pg_listener: failed to parse payload: %r", raw[:200])
            return

        if self._refetch_elided and event.kind == "set" and event.value is None:
            event = self._maybe_refetch(event)

        try:
            self._on_event(event)
        except Exception:
            _log.exception("pg_listener: subscriber raised")

    def _maybe_refetch(self, event: MemoryEvent) -> MemoryEvent:
        """Refill ``value`` for elided KV events by reading the store."""
        if self._user is None or event.source != "kv":
            return event
        try:
            from obscura.memory import MemoryStore

            store = MemoryStore.for_user(self._user)
            value = store.get(event.key.key, namespace=event.key.namespace)
        except Exception:
            _log.exception("pg_listener: refetch failed for %s", event.key)
            return event
        # MemoryEvent is frozen — rebuild with the refetched value.
        return MemoryEvent(
            kind=event.kind,
            key=event.key,
            value=value,
            ttl_seconds=event.ttl_seconds,
            source=event.source,
            user_id=event.user_id,
            at=event.at,
            event_id=event.event_id,
            event_uuid=event.event_uuid,
        )

    def _run(self) -> None:
        backoff = _RECONNECT_INITIAL_SECONDS
        while not self._stop.is_set():
            try:
                self._listen_forever()
                backoff = _RECONNECT_INITIAL_SECONDS
            except Exception:
                _log.exception(
                    "pg_listener: connection error, retrying in %.1fs",
                    backoff,
                )
                # Interruptible sleep so stop() is prompt
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, _RECONNECT_MAX_SECONDS)

    def _listen_forever(self) -> None:
        """Open a connection, LISTEN, loop until stop or error."""
        try:
            import psycopg2
            from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

            from obscura.core.pg_config import PGConfig
        except ImportError as exc:
            msg = "pg_listener requires psycopg2; install the server/pg extras"
            raise RuntimeError(msg) from exc

        cfg = PGConfig.from_env()
        conn = psycopg2.connect(
            host=cfg.host,
            port=cfg.port,
            dbname=cfg.database,
            user=cfg.user,
            password=cfg.password,
        )
        try:
            conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            with conn.cursor() as cur:
                # channel identifier is not user-controlled at this point;
                # we hand-quote rather than parametrize (LISTEN doesn't
                # accept bind parameters).
                cur.execute(f'LISTEN "{self._channel}"')
            _log.info("pg_listener: listening on %s", self._channel)

            while not self._stop.is_set():
                # Wait up to 1s for activity, then loop so stop is prompt.
                readable, _, _ = select.select([conn], [], [], 1.0)
                if not readable:
                    continue
                conn.poll()
                while conn.notifies:
                    notify = conn.notifies.pop(0)
                    self.handle_payload(notify.payload)
        finally:
            try:
                conn.close()
            except Exception:
                _log.debug("pg_listener: close raised", exc_info=True)


def listen_blocking(
    *,
    on_event: Callable[[MemoryEvent], None],
    channel: str = "obscura_memory",
    refetch_elided: bool = False,
    user: AuthenticatedUser | None = None,
) -> None:
    """Run a listener in the foreground until KeyboardInterrupt.

    Convenience for CLI consumers / demos. For a long-running service, use
    :class:`PgEventListener` directly so you can stop it cleanly on shutdown.
    """
    listener = PgEventListener(
        on_event=on_event,
        channel=channel,
        refetch_elided=refetch_elided,
        user=user,
    )
    listener.start()
    try:
        while listener.is_running():
            time.sleep(0.5)
    except KeyboardInterrupt:
        _log.debug("suppressed exception in listen_blocking", exc_info=True)
    finally:
        listener.stop()
