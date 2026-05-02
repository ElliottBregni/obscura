"""obscura.core.throttle — Backend call throttling for OAuth-friendly fan-out.

Wraps each :class:`~obscura.core.types.BackendProtocol` with a per-backend
gate that enforces three layers, all *waiting* (never raising):

  1. **Concurrency cap** — asyncio semaphore limits in-flight calls.
  2. **Rate limit** — token bucket smooths bursts into a req/min budget.
  3. **Priority queue** — interactive REPL turns admit ahead of daemon ticks.

OAuth sessions (Claude Pro, ChatGPT Plus, Copilot) bill quota against a
single account — fanning out daemons would trip upstream 429s. The throttle
shapes traffic at the client *before* it leaves, keeping a fleet of agents
within the session's quota without errors.

Configuration (all per-backend; env vars are case-insensitive):

    OBSCURA_THROTTLE_ENABLED              # global on/off (default: on)
    OBSCURA_THROTTLE_<BACKEND>_CONCURRENT # max in-flight calls
    OBSCURA_THROTTLE_<BACKEND>_RPM        # requests per minute (0 = unlimited)
    OBSCURA_THROTTLE_<BACKEND>_ENABLED    # per-backend on/off
    OBSCURA_THROTTLE_<BACKEND>_MAX_WAIT_S # warn beyond this many seconds

Defaults are tuned for typical OAuth subscription quotas; override any
backend by exporting the relevant env vars or by mutating
:data:`DEFAULT_LIMITS` before the first backend is created.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import hashlib
import heapq
import itertools
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from obscura.core.auth import AuthConfig
    from obscura.core.tools import ToolRegistry
    from obscura.core.types import (
        BackendCapabilities,
        BackendProtocol,
        HookPoint,
        Message,
        NativeHandle,
        SessionRef,
        StreamChunk,
        ToolSpec,
    )

logger = logging.getLogger("obscura.throttle")


# ---------------------------------------------------------------------------
# Priority levels
# ---------------------------------------------------------------------------


class Priority(enum.IntEnum):
    """Higher value admits first.

    INTERACTIVE — REPL turns, user-facing requests (default).
    DAEMON      — KAIROS ticks, background agents, batch jobs.
    """

    DAEMON = 0
    INTERACTIVE = 10


_PRIORITY_KWARG_KEYS = (
    "__obscura_priority__",
    "obscura_priority",
    "priority_hint",
)


def coerce_priority(value: Any) -> Priority:
    """Map a string/int/Priority into a :class:`Priority`."""
    if isinstance(value, Priority):
        return value
    if value is None:
        return Priority.INTERACTIVE
    if isinstance(value, int):
        return (
            Priority.INTERACTIVE
            if value >= int(Priority.INTERACTIVE)
            else Priority.DAEMON
        )
    text = str(value).strip().lower()
    if text in ("daemon", "background", "kairos", "low", "batch"):
        return Priority.DAEMON
    return Priority.INTERACTIVE


# ---------------------------------------------------------------------------
# Limits — per-backend configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThrottleLimits:
    """Per-backend throttle configuration."""

    max_concurrent: int = 3
    requests_per_minute: float = 60.0  # 0 disables the rate limit
    enabled: bool = True
    max_wait_seconds: float = 300.0  # warn beyond this; never raises


# Defaults tuned for typical OAuth subscription quotas. Override per backend
# via env vars or by mutating this dict before client creation.
DEFAULT_LIMITS: dict[str, ThrottleLimits] = {
    "claude": ThrottleLimits(max_concurrent=5, requests_per_minute=20.0),
    "codex": ThrottleLimits(max_concurrent=5, requests_per_minute=30.0),
    "copilot": ThrottleLimits(max_concurrent=6, requests_per_minute=60.0),
    "openai": ThrottleLimits(max_concurrent=10, requests_per_minute=500.0),
    "moonshot": ThrottleLimits(max_concurrent=5, requests_per_minute=60.0),
    "localllm": ThrottleLimits(max_concurrent=64, requests_per_minute=0.0),
}


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return default
    return raw not in ("0", "false", "no", "off")


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default %s", name, raw, default)
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default %s", name, raw, default)
        return default


def limits_for(backend: str) -> ThrottleLimits:
    """Resolve effective limits for *backend* from env + :data:`DEFAULT_LIMITS`."""
    key = backend.lower()
    base = DEFAULT_LIMITS.get(key) or ThrottleLimits()
    upper = key.upper()
    return ThrottleLimits(
        max_concurrent=_int_env(
            f"OBSCURA_THROTTLE_{upper}_CONCURRENT",
            base.max_concurrent,
        ),
        requests_per_minute=_float_env(
            f"OBSCURA_THROTTLE_{upper}_RPM",
            base.requests_per_minute,
        ),
        enabled=_bool_env(
            f"OBSCURA_THROTTLE_{upper}_ENABLED",
            base.enabled,
        ),
        max_wait_seconds=_float_env(
            f"OBSCURA_THROTTLE_{upper}_MAX_WAIT_S",
            base.max_wait_seconds,
        ),
    )


def globally_enabled() -> bool:
    """Return False when the throttle layer is globally disabled."""
    return _bool_env("OBSCURA_THROTTLE_ENABLED", default=True)


# ---------------------------------------------------------------------------
# Token bucket — rate limit
# ---------------------------------------------------------------------------


class TokenBucket:
    """Async token bucket with monotonic refill.

    ``capacity`` tokens, refilled at ``rate`` tokens/second. When
    ``rate_per_minute <= 0`` the bucket is disabled (no-op).
    """

    def __init__(
        self,
        *,
        rate_per_minute: float,
        burst: float | None = None,
    ) -> None:
        self._rate_per_sec = max(rate_per_minute, 0.0) / 60.0
        # Burst defaults to one minute's worth (rounded up to >= 1) so we
        # absorb short spikes without dropping under the steady-state rate.
        self._capacity = float(
            burst if burst is not None else max(rate_per_minute, 1.0),
        )
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def disabled(self) -> bool:
        return self._rate_per_sec <= 0.0

    async def acquire(self) -> float:
        """Wait until a token is available, take one, return seconds waited."""
        if self.disabled:
            return 0.0
        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity,
                    self._tokens + (now - self._last) * self._rate_per_sec,
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited
                deficit = 1.0 - self._tokens
                wait_s = deficit / self._rate_per_sec
            await asyncio.sleep(wait_s)
            waited += wait_s


# ---------------------------------------------------------------------------
# Priority semaphore — admits high priority first
# ---------------------------------------------------------------------------


class _PrioritySemaphore:
    """Semaphore where waiters are admitted in priority order, FIFO within."""

    def __init__(self, capacity: int) -> None:
        self._capacity = max(capacity, 1)
        self._available = self._capacity
        # heap of (-priority, seq, future); negative for max-heap behaviour.
        self._waiters: list[tuple[int, int, asyncio.Future[None]]] = []
        self._counter = itertools.count()

    @property
    def available(self) -> int:
        return self._available

    async def acquire(self, priority: Priority) -> None:
        if self._available > 0 and not self._waiters:
            self._available -= 1
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(
            self._waiters,
            (-int(priority), next(self._counter), fut),
        )
        try:
            await fut
        except asyncio.CancelledError:
            self._waiters = [e for e in self._waiters if e[2] is not fut]
            heapq.heapify(self._waiters)
            if fut.done() and not fut.cancelled():
                # Granted just before cancel — return the slot.
                self.release()
            raise

    def release(self) -> None:
        # Wake highest-priority pending waiter, else return slot.
        while self._waiters:
            _neg_pri, _seq, fut = heapq.heappop(self._waiters)
            if fut.cancelled() or fut.done():
                continue
            fut.set_result(None)
            return
        self._available += 1


# ---------------------------------------------------------------------------
# Per-backend gate (semaphore + token bucket)
# ---------------------------------------------------------------------------


class BackendThrottle:
    """Combined concurrency + rate gate for one backend/auth pair."""

    def __init__(self, name: str, limits: ThrottleLimits) -> None:
        self.name = name
        self.limits = limits
        self._sem: _PrioritySemaphore | None = (
            _PrioritySemaphore(limits.max_concurrent) if limits.enabled else None
        )
        self._bucket: TokenBucket | None = (
            TokenBucket(rate_per_minute=limits.requests_per_minute)
            if limits.enabled and limits.requests_per_minute > 0
            else None
        )

    @contextlib.asynccontextmanager
    async def gate(
        self,
        priority: Priority = Priority.INTERACTIVE,
    ) -> AsyncIterator[None]:
        """Acquire a slot (graceful wait), yield, release on exit."""
        if self._sem is None:
            yield
            return
        start = time.monotonic()
        await self._sem.acquire(priority)
        try:
            if self._bucket is not None:
                await self._bucket.acquire()
            wait_s = time.monotonic() - start
            if wait_s > self.limits.max_wait_seconds:
                logger.warning(
                    "throttle: %s gate held for %.1fs (>soft limit %.1fs)",
                    self.name,
                    wait_s,
                    self.limits.max_wait_seconds,
                )
            yield
        finally:
            self._sem.release()


# ---------------------------------------------------------------------------
# Auth fingerprinting — distinguishes credentials sharing a backend
# ---------------------------------------------------------------------------


def auth_fingerprint(auth: AuthConfig | None) -> str:
    """Stable 16-char fingerprint of an :class:`AuthConfig`.

    Two configs with the same effective credentials share a throttle.
    Returns ``"oauth"`` when no credential material is present (pure
    OAuth-session backends like Codex with a ChatGPT login).
    """
    if auth is None:
        return "default"
    parts = (
        auth.github_token,
        auth.oauth_github_token,
        auth.anthropic_api_key,
        auth.openai_api_key,
        auth.moonshot_api_key,
        auth.openai_base_url,
        auth.localllm_base_url,
    )
    seed = "|".join(p or "" for p in parts)
    if not seed.strip("|"):
        return "oauth"
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Registry — one BackendThrottle per (backend, auth)
# ---------------------------------------------------------------------------


_REGISTRY: dict[tuple[str, str], BackendThrottle] = {}


def get_throttle(backend_name: str, auth_id: str) -> BackendThrottle:
    """Return the shared :class:`BackendThrottle` for this (backend, auth)."""
    key = (backend_name.lower(), auth_id)
    existing = _REGISTRY.get(key)
    if existing is not None:
        return existing
    throttle = BackendThrottle(backend_name.lower(), limits_for(backend_name))
    _REGISTRY[key] = throttle
    return throttle


def reset_registry() -> None:
    """Clear all registered throttles. For tests."""
    _REGISTRY.clear()


# ---------------------------------------------------------------------------
# ThrottledBackend — BackendProtocol decorator
# ---------------------------------------------------------------------------


class ThrottledBackend:
    """Wraps a :class:`BackendProtocol` with throttle gating on send/stream."""

    def __init__(
        self,
        wrapped: BackendProtocol,
        *,
        backend_name: str,
        throttle: BackendThrottle,
    ) -> None:
        self._wrapped = wrapped
        self._backend_name = backend_name
        self._throttle = throttle

    @property
    def wrapped(self) -> BackendProtocol:
        return self._wrapped

    @property
    def throttle(self) -> BackendThrottle:
        return self._throttle

    @staticmethod
    def _pop_priority(kwargs: dict[str, Any]) -> Priority:
        for key in _PRIORITY_KWARG_KEYS:
            if key in kwargs:
                return coerce_priority(kwargs.pop(key))
        env = os.environ.get("OBSCURA_CALL_PRIORITY", "").strip()
        if env:
            return coerce_priority(env)
        return Priority.INTERACTIVE

    # -- Lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await self._wrapped.start()

    async def stop(self) -> None:
        await self._wrapped.stop()

    # -- Throttled entry points --------------------------------------------

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        priority = self._pop_priority(kwargs)
        async with self._throttle.gate(priority):
            return await self._wrapped.send(prompt, **kwargs)

    async def stream(
        self,
        prompt: str,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        priority = self._pop_priority(kwargs)
        async with self._throttle.gate(priority):
            async for chunk in self._wrapped.stream(prompt, **kwargs):
                yield chunk

    # -- Passthrough -------------------------------------------------------

    async def create_session(self, **kwargs: Any) -> SessionRef:
        return await self._wrapped.create_session(**kwargs)

    async def resume_session(self, ref: SessionRef) -> None:
        await self._wrapped.resume_session(ref)

    async def list_sessions(self) -> list[SessionRef]:
        return await self._wrapped.list_sessions()

    async def delete_session(self, ref: SessionRef) -> None:
        await self._wrapped.delete_session(ref)

    def register_tool(self, spec: ToolSpec) -> None:
        self._wrapped.register_tool(spec)

    def register_hook(
        self,
        hook: HookPoint,
        callback: Callable[..., Any],
    ) -> None:
        self._wrapped.register_hook(hook, callback)

    def get_tool_registry(self) -> ToolRegistry:
        return self._wrapped.get_tool_registry()

    @property
    def native(self) -> NativeHandle:
        return self._wrapped.native

    def capabilities(self) -> BackendCapabilities:
        return self._wrapped.capabilities()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.__dict__["_wrapped"], name)


# ---------------------------------------------------------------------------
# Factory — wire-in point used by Client._create_backend
# ---------------------------------------------------------------------------


def wrap_if_enabled(
    backend: BackendProtocol,
    *,
    backend_name: str,
    auth: AuthConfig | None,
) -> BackendProtocol:
    """Return *backend* wrapped in :class:`ThrottledBackend` when enabled."""
    if not globally_enabled():
        return backend
    limits = limits_for(backend_name)
    if not limits.enabled:
        return backend
    throttle = get_throttle(backend_name, auth_fingerprint(auth))
    return ThrottledBackend(
        backend,
        backend_name=backend_name,
        throttle=throttle,
    )
