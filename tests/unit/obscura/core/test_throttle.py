"""Tests for obscura.core.throttle — backend call throttling."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from unittest.mock import patch

import pytest

from obscura.core.auth import AuthConfig
from obscura.core.throttle import (
    DEFAULT_LIMITS,
    BackendThrottle,
    Priority,
    ThrottledBackend,
    ThrottleLimits,
    TokenBucket,
    _PrioritySemaphore,
    auth_fingerprint,
    coerce_priority,
    get_throttle,
    globally_enabled,
    limits_for,
    reset_registry,
    wrap_if_enabled,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    reset_registry()
    yield
    reset_registry()


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


class TestPriority:
    def test_interactive_higher_than_daemon(self) -> None:
        assert int(Priority.INTERACTIVE) > int(Priority.DAEMON)

    def test_coerce_returns_priority_unchanged(self) -> None:
        assert coerce_priority(Priority.DAEMON) is Priority.DAEMON

    def test_coerce_default_is_interactive(self) -> None:
        assert coerce_priority(None) is Priority.INTERACTIVE

    def test_coerce_string_daemon(self) -> None:
        assert coerce_priority("daemon") is Priority.DAEMON
        assert coerce_priority("background") is Priority.DAEMON
        assert coerce_priority("kairos") is Priority.DAEMON

    def test_coerce_string_interactive(self) -> None:
        assert coerce_priority("interactive") is Priority.INTERACTIVE
        assert coerce_priority("user") is Priority.INTERACTIVE

    def test_coerce_unknown_defaults_to_interactive(self) -> None:
        assert coerce_priority("garbage") is Priority.INTERACTIVE


# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------


class TestTokenBucket:
    @pytest.mark.asyncio
    async def test_disabled_when_rate_zero(self) -> None:
        bucket = TokenBucket(rate_per_minute=0)
        assert bucket.disabled is True
        assert await bucket.acquire() == 0.0

    @pytest.mark.asyncio
    async def test_first_acquire_immediate(self) -> None:
        bucket = TokenBucket(rate_per_minute=60.0)
        waited = await bucket.acquire()
        assert waited == 0.0

    @pytest.mark.asyncio
    async def test_burst_capacity(self) -> None:
        bucket = TokenBucket(rate_per_minute=60.0)
        # capacity defaults to one minute's worth = 60 tokens.
        for _ in range(5):
            assert await bucket.acquire() == 0.0

    @pytest.mark.asyncio
    async def test_waits_when_empty(self) -> None:
        # 600/min = 10/sec → after draining burst, next token in ~0.1s.
        bucket = TokenBucket(rate_per_minute=600.0, burst=2)
        await bucket.acquire()
        await bucket.acquire()
        start = time.monotonic()
        waited = await bucket.acquire()
        elapsed = time.monotonic() - start
        assert waited > 0.0
        assert elapsed >= 0.05  # should have waited at least ~100ms


# ---------------------------------------------------------------------------
# Priority semaphore
# ---------------------------------------------------------------------------


class TestPrioritySemaphore:
    @pytest.mark.asyncio
    async def test_capacity_respected(self) -> None:
        sem = _PrioritySemaphore(2)
        await sem.acquire(Priority.INTERACTIVE)
        await sem.acquire(Priority.INTERACTIVE)
        assert sem.available == 0

    @pytest.mark.asyncio
    async def test_release_returns_slot(self) -> None:
        sem = _PrioritySemaphore(1)
        await sem.acquire(Priority.INTERACTIVE)
        sem.release()
        # The released slot is now free; another acquire should succeed.
        await sem.acquire(Priority.INTERACTIVE)

    @pytest.mark.asyncio
    async def test_release_wakes_waiter(self) -> None:
        sem = _PrioritySemaphore(1)
        await sem.acquire(Priority.INTERACTIVE)

        order: list[str] = []

        async def waiter() -> None:
            await sem.acquire(Priority.INTERACTIVE)
            order.append("woke")

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.01)  # let waiter queue up
        assert order == []
        sem.release()
        await asyncio.wait_for(task, timeout=1.0)
        assert order == ["woke"]

    @pytest.mark.asyncio
    async def test_priority_admits_interactive_first(self) -> None:
        """Interactive waiters jump ahead of daemon waiters."""
        sem = _PrioritySemaphore(1)
        await sem.acquire(Priority.INTERACTIVE)  # block the slot

        order: list[str] = []
        ready = asyncio.Event()
        ready_count = {"n": 0}

        async def waiter(label: str, priority: Priority) -> None:
            ready_count["n"] += 1
            if ready_count["n"] == 2:
                ready.set()
            await sem.acquire(priority)
            order.append(label)
            sem.release()

        # Queue daemon first, then interactive — interactive should win.
        d_task = asyncio.create_task(waiter("daemon", Priority.DAEMON))
        i_task = asyncio.create_task(waiter("interactive", Priority.INTERACTIVE))
        await ready.wait()
        await asyncio.sleep(0.01)  # ensure both are queued
        sem.release()
        await asyncio.gather(d_task, i_task)
        assert order == ["interactive", "daemon"]

    @pytest.mark.asyncio
    async def test_fifo_within_priority(self) -> None:
        sem = _PrioritySemaphore(1)
        await sem.acquire(Priority.INTERACTIVE)

        order: list[str] = []

        async def waiter(label: str) -> None:
            await sem.acquire(Priority.INTERACTIVE)
            order.append(label)
            sem.release()

        t1 = asyncio.create_task(waiter("a"))
        await asyncio.sleep(0.01)
        t2 = asyncio.create_task(waiter("b"))
        await asyncio.sleep(0.01)
        sem.release()
        await asyncio.gather(t1, t2)
        assert order == ["a", "b"]


# ---------------------------------------------------------------------------
# BackendThrottle
# ---------------------------------------------------------------------------


class TestBackendThrottle:
    @pytest.mark.asyncio
    async def test_gate_serializes_when_concurrent_one(self) -> None:
        throttle = BackendThrottle(
            "test",
            ThrottleLimits(max_concurrent=1, requests_per_minute=0),
        )
        order: list[str] = []

        async def caller(label: str) -> None:
            async with throttle.gate():
                order.append(f"{label}-enter")
                await asyncio.sleep(0.02)
                order.append(f"{label}-exit")

        await asyncio.gather(caller("a"), caller("b"))
        # b cannot enter while a is inside.
        assert order.index("a-exit") < order.index("b-enter")

    @pytest.mark.asyncio
    async def test_gate_passthrough_when_disabled(self) -> None:
        throttle = BackendThrottle(
            "test",
            ThrottleLimits(max_concurrent=1, enabled=False),
        )
        async with throttle.gate():
            async with throttle.gate():  # no contention because disabled
                pass

    @pytest.mark.asyncio
    async def test_gate_priority_admits_interactive_first(self) -> None:
        throttle = BackendThrottle(
            "test",
            ThrottleLimits(max_concurrent=1, requests_per_minute=0),
        )
        order: list[str] = []
        gate_held = asyncio.Event()
        release_now = asyncio.Event()

        async def holder() -> None:
            async with throttle.gate():
                gate_held.set()
                await release_now.wait()

        async def caller(label: str, priority: Priority) -> None:
            async with throttle.gate(priority):
                order.append(label)

        h = asyncio.create_task(holder())
        await gate_held.wait()
        d = asyncio.create_task(caller("daemon", Priority.DAEMON))
        await asyncio.sleep(0.01)
        i = asyncio.create_task(caller("interactive", Priority.INTERACTIVE))
        await asyncio.sleep(0.01)
        release_now.set()
        await asyncio.gather(h, d, i)
        assert order == ["interactive", "daemon"]


# ---------------------------------------------------------------------------
# Auth fingerprint
# ---------------------------------------------------------------------------


class TestAuthFingerprint:
    def test_none_returns_default(self) -> None:
        assert auth_fingerprint(None) == "default"

    def test_empty_returns_oauth(self) -> None:
        assert auth_fingerprint(AuthConfig()) == "oauth"

    def test_distinguishes_keys(self) -> None:
        a = AuthConfig(anthropic_api_key="key-a")
        b = AuthConfig(anthropic_api_key="key-b")
        assert auth_fingerprint(a) != auth_fingerprint(b)

    def test_same_key_same_fingerprint(self) -> None:
        a = AuthConfig(anthropic_api_key="key-a")
        b = AuthConfig(anthropic_api_key="key-a")
        assert auth_fingerprint(a) == auth_fingerprint(b)


# ---------------------------------------------------------------------------
# Limits / config
# ---------------------------------------------------------------------------


class TestLimitsConfig:
    def test_defaults_for_known_backend(self) -> None:
        limits = limits_for("claude")
        assert limits.max_concurrent == DEFAULT_LIMITS["claude"].max_concurrent
        assert limits.enabled is True

    def test_unknown_backend_falls_back(self) -> None:
        limits = limits_for("nonsense")
        assert limits.enabled is True
        assert limits.max_concurrent >= 1

    def test_env_override_concurrent(self) -> None:
        with patch.dict(os.environ, {"OBSCURA_THROTTLE_CLAUDE_CONCURRENT": "7"}):
            assert limits_for("claude").max_concurrent == 7

    def test_env_override_rpm(self) -> None:
        with patch.dict(os.environ, {"OBSCURA_THROTTLE_CODEX_RPM": "12.5"}):
            assert limits_for("codex").requests_per_minute == 12.5

    def test_env_override_disable(self) -> None:
        with patch.dict(os.environ, {"OBSCURA_THROTTLE_OPENAI_ENABLED": "false"}):
            assert limits_for("openai").enabled is False

    def test_global_enabled_default_true(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OBSCURA_THROTTLE_ENABLED", None)
            assert globally_enabled() is True

    def test_global_disable(self) -> None:
        with patch.dict(os.environ, {"OBSCURA_THROTTLE_ENABLED": "false"}):
            assert globally_enabled() is False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_returns_same_instance(self) -> None:
        a = get_throttle("claude", "abc")
        b = get_throttle("claude", "abc")
        assert a is b

    def test_distinct_instances_per_auth(self) -> None:
        a = get_throttle("claude", "abc")
        b = get_throttle("claude", "xyz")
        assert a is not b

    def test_distinct_instances_per_backend(self) -> None:
        a = get_throttle("claude", "abc")
        b = get_throttle("codex", "abc")
        assert a is not b


# ---------------------------------------------------------------------------
# ThrottledBackend wrapper
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Minimal BackendProtocol stand-in for the throttle wrapper tests."""

    def __init__(self) -> None:
        self.send_calls: list[tuple[str, dict[str, Any]]] = []
        self.stream_calls: list[tuple[str, dict[str, Any]]] = []
        self.send_response = "ok"
        self.chunks: list[str] = ["a", "b", "c"]

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, prompt: str, **kwargs: Any) -> str:
        self.send_calls.append((prompt, dict(kwargs)))
        return self.send_response

    async def stream(self, prompt: str, **kwargs: Any) -> Any:
        self.stream_calls.append((prompt, dict(kwargs)))
        for chunk in self.chunks:
            yield chunk

    async def create_session(self, **kwargs: Any) -> str:
        return "session-1"

    async def resume_session(self, ref: Any) -> None:
        pass

    async def list_sessions(self) -> list[Any]:
        return []

    async def delete_session(self, ref: Any) -> None:
        pass

    def register_tool(self, spec: Any) -> None:
        pass

    def register_hook(self, hook: Any, callback: Any) -> None:
        pass

    def get_tool_registry(self) -> Any:
        return None

    @property
    def native(self) -> Any:
        return self

    def capabilities(self) -> Any:
        return None


class TestThrottledBackend:
    def _make(
        self,
        limits: ThrottleLimits | None = None,
    ) -> tuple[ThrottledBackend, _FakeBackend]:
        fake = _FakeBackend()
        throttle = BackendThrottle(
            "test",
            limits or ThrottleLimits(max_concurrent=2, requests_per_minute=0),
        )
        wrapped = ThrottledBackend(fake, backend_name="test", throttle=throttle)
        return wrapped, fake

    @pytest.mark.asyncio
    async def test_send_calls_wrapped(self) -> None:
        wrapped, fake = self._make()
        result = await wrapped.send("hi")
        assert result == "ok"
        assert fake.send_calls == [("hi", {})]

    @pytest.mark.asyncio
    async def test_stream_yields_chunks(self) -> None:
        wrapped, fake = self._make()
        chunks = [c async for c in wrapped.stream("hi")]
        assert chunks == ["a", "b", "c"]
        assert fake.stream_calls == [("hi", {})]

    @pytest.mark.asyncio
    async def test_priority_kwarg_stripped_before_forward(self) -> None:
        wrapped, fake = self._make()
        await wrapped.send("hi", __obscura_priority__="daemon")
        assert "__obscura_priority__" not in fake.send_calls[0][1]

    @pytest.mark.asyncio
    async def test_priority_alt_kwargs_stripped(self) -> None:
        wrapped, fake = self._make()
        await wrapped.send("a", obscura_priority="daemon")
        await wrapped.send("b", priority_hint="daemon")
        assert "obscura_priority" not in fake.send_calls[0][1]
        assert "priority_hint" not in fake.send_calls[1][1]

    @pytest.mark.asyncio
    async def test_passthrough_kwargs_preserved(self) -> None:
        wrapped, fake = self._make()
        await wrapped.send("hi", model="gpt-4o", session_id="abc")
        assert fake.send_calls[0][1] == {"model": "gpt-4o", "session_id": "abc"}

    @pytest.mark.asyncio
    async def test_concurrency_cap(self) -> None:
        wrapped, fake = self._make(
            ThrottleLimits(max_concurrent=1, requests_per_minute=0),
        )

        order: list[str] = []

        async def slow_send(prompt: str, **kwargs: Any) -> str:
            order.append(f"{prompt}-enter")
            await asyncio.sleep(0.02)
            order.append(f"{prompt}-exit")
            return "ok"

        fake.send = slow_send  # type: ignore[method-assign]

        await asyncio.gather(wrapped.send("a"), wrapped.send("b"))
        assert order.index("a-exit") < order.index("b-enter") or order.index(
            "b-exit",
        ) < order.index("a-enter")


# ---------------------------------------------------------------------------
# wrap_if_enabled factory
# ---------------------------------------------------------------------------


class TestWrapIfEnabled:
    def test_returns_unwrapped_when_globally_disabled(self) -> None:
        fake = _FakeBackend()
        with patch.dict(os.environ, {"OBSCURA_THROTTLE_ENABLED": "false"}):
            result = wrap_if_enabled(
                fake,  # type: ignore[arg-type]
                backend_name="claude",
                auth=None,
            )
        assert result is fake

    def test_returns_unwrapped_when_per_backend_disabled(self) -> None:
        fake = _FakeBackend()
        with patch.dict(os.environ, {"OBSCURA_THROTTLE_CLAUDE_ENABLED": "false"}):
            result = wrap_if_enabled(
                fake,  # type: ignore[arg-type]
                backend_name="claude",
                auth=None,
            )
        assert result is fake

    def test_wraps_when_enabled(self) -> None:
        fake = _FakeBackend()
        result = wrap_if_enabled(
            fake,  # type: ignore[arg-type]
            backend_name="claude",
            auth=AuthConfig(anthropic_api_key="k"),
        )
        assert isinstance(result, ThrottledBackend)
        assert result.wrapped is fake

    def test_same_auth_shares_throttle(self) -> None:
        f1, f2 = _FakeBackend(), _FakeBackend()
        auth = AuthConfig(anthropic_api_key="same")
        w1 = wrap_if_enabled(f1, backend_name="claude", auth=auth)  # type: ignore[arg-type]
        w2 = wrap_if_enabled(f2, backend_name="claude", auth=auth)  # type: ignore[arg-type]
        assert isinstance(w1, ThrottledBackend)
        assert isinstance(w2, ThrottledBackend)
        assert w1.throttle is w2.throttle

    def test_different_auth_separate_throttle(self) -> None:
        f1, f2 = _FakeBackend(), _FakeBackend()
        w1 = wrap_if_enabled(
            f1,  # type: ignore[arg-type]
            backend_name="claude",
            auth=AuthConfig(anthropic_api_key="a"),
        )
        w2 = wrap_if_enabled(
            f2,  # type: ignore[arg-type]
            backend_name="claude",
            auth=AuthConfig(anthropic_api_key="b"),
        )
        assert isinstance(w1, ThrottledBackend)
        assert isinstance(w2, ThrottledBackend)
        assert w1.throttle is not w2.throttle
