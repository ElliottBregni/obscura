"""obscura.memory.reaper — Background expiration reaper for KV memory.

The KV lazy-expire path in :meth:`MemoryStore.get` only fires an ``expire``
event when someone reads an expired key. The reaper closes the gap: it scans
all live stores on an interval and emits events for rows whose TTL has passed,
even if no one reads them.

Enable via env:

- ``OBSCURA_MEMORY_REAPER=1`` at import time auto-starts the reaper.
- ``OBSCURA_MEMORY_REAPER_INTERVAL`` (seconds, default 30) controls cadence.

Or start programmatically::

    from obscura.memory.reaper import start_reaper
    reaper = start_reaper(interval_seconds=10)
    ...
    reaper.stop()

Only stores registered in ``MemoryStore._instances`` (constructed via
``MemoryStore.for_user``) plus the ``GlobalMemoryStore`` singleton are scanned.
Stores built by direct construction with a custom ``db_path`` are not tracked
— call :meth:`MemoryStore.reap_expired` on those manually.

Vector memory is intentionally out of scope: decay is continuous, so
``expire`` is ill-defined. Use ``VectorMemoryStore.run_maintenance`` for that.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

from obscura.memory import GlobalMemoryStore, MemoryStore

if TYPE_CHECKING:
    from collections.abc import Iterable

_log = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 30.0


def _live_stores() -> Iterable[MemoryStore]:
    """Iterate every ``MemoryStore`` the reaper knows about."""
    # Snapshot to avoid holding the class lock while scanning.
    with MemoryStore._lock:  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        stores: list[MemoryStore] = list(
            MemoryStore._instances.values(),  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        )
    global_inst = GlobalMemoryStore._instance  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    if global_inst is not None:
        stores.append(global_inst)
    return stores


class ExpirationReaper:
    """Daemon thread that periodically reaps expired rows across all stores.

    The reaper walks each live :class:`MemoryStore` once per tick, calls
    :meth:`MemoryStore.reap_expired`, and logs the counts. Exceptions from a
    single store don't abort the tick; the reaper logs and continues.
    """

    def __init__(self, interval_seconds: float = _DEFAULT_INTERVAL_SECONDS) -> None:
        if interval_seconds <= 0:
            msg = f"interval_seconds must be positive, got {interval_seconds}"
            raise ValueError(msg)
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the reaper thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="obscura-memory-reaper",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the reaper to stop and wait briefly for it to finish."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_running(self) -> bool:
        """Return True if the reaper thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def tick_once(self) -> int:
        """Reap across all live stores once. Returns total rows reaped."""
        total = 0
        for store in _live_stores():
            try:
                total += store.reap_expired()
            except Exception:
                _log.exception(
                    "reaper: failed to reap store user_id=%s",
                    getattr(store, "user_id", "?"),
                )
        return total

    def _run(self) -> None:
        _log.info(
            "memory reaper starting (interval=%.1fs)",
            self._interval,
        )
        while not self._stop.is_set():
            try:
                reaped = self.tick_once()
                if reaped:
                    _log.debug("memory reaper: reaped %d rows", reaped)
            except Exception:
                # Defensive: tick_once already handles per-store errors,
                # but a bug in the iteration itself shouldn't kill the thread.
                _log.exception("memory reaper: tick failed")
            # Use Event.wait for interruptible sleep
            self._stop.wait(self._interval)
        _log.info("memory reaper stopped")


_reaper: ExpirationReaper | None = None
_reaper_lock = threading.Lock()


def start_reaper(
    interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
) -> ExpirationReaper:
    """Return the module-level reaper, starting it if needed.

    Subsequent calls return the existing reaper — the ``interval_seconds``
    argument is only used on first construction.
    """
    global _reaper
    with _reaper_lock:
        if _reaper is None:
            _reaper = ExpirationReaper(interval_seconds=interval_seconds)
        _reaper.start()
        return _reaper


def stop_reaper() -> None:
    """Stop the module-level reaper if it's running."""
    global _reaper
    with _reaper_lock:
        if _reaper is not None:
            _reaper.stop()
            _reaper = None


def _maybe_autostart() -> None:
    """Start the reaper at import time if ``OBSCURA_MEMORY_REAPER=1``."""
    if os.environ.get("OBSCURA_MEMORY_REAPER", "").strip().lower() not in (
        "1",
        "true",
        "on",
        "yes",
    ):
        return
    try:
        interval = float(
            os.environ.get(
                "OBSCURA_MEMORY_REAPER_INTERVAL",
                str(_DEFAULT_INTERVAL_SECONDS),
            ),
        )
    except ValueError:
        _log.warning(
            "invalid OBSCURA_MEMORY_REAPER_INTERVAL; using default %s",
            _DEFAULT_INTERVAL_SECONDS,
        )
        interval = _DEFAULT_INTERVAL_SECONDS
    start_reaper(interval_seconds=interval)


_maybe_autostart()
