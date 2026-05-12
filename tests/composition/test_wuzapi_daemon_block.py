"""Tests for `install_wuzapi_daemon` — wuzapi inbound bridge (REPL only).

Covers the auto-promotion machinery added to make multi-REPL "just work":
the port-acquire helper and the background promotion watcher.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.composition.blocks import wuzapi_daemon
from obscura.composition.session import (
    AgentSession,
    SessionConfig,
    new_session_id,
)


class _StubClient:
    def __init__(self) -> None:
        from obscura.core.tools import ToolRegistry

        self._tool_registry = ToolRegistry()
        self._backend = self

    def register_tool(self, spec: Any) -> None:
        pass


def _make_session() -> AgentSession:
    return AgentSession(
        session_id=new_session_id(),
        surface="repl",
        config=SessionConfig(),
        client=_StubClient(),  # type: ignore[arg-type]
    )


def _free_port() -> int:
    """Ask the kernel for an ephemeral port, then close so it's free.

    A microsecond race exists between close + the test's bind attempt,
    but it's vanishingly rare on a loopback unit-test run.
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.unit
@pytest.mark.asyncio
async def test_try_acquire_returns_false_when_port_taken() -> None:
    """When something else owns the port, the helper returns False and
    registers no resource — caller knows to stay in peer mode."""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    port: int = blocker.getsockname()[1]
    try:
        session = _make_session()
        result = await wuzapi_daemon._try_acquire_port_and_start_service(
            session, port,
        )
        assert result is False
        assert len(session._resources) == 0
    finally:
        blocker.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_try_acquire_registers_resource_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the port is free and service starts, the context manager is
    registered for LIFO teardown."""
    session = _make_session()
    port = _free_port()

    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=None)
    fake_cm.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr(
        "obscura.integrations.whatsapp.wuzapi.service.wuzapi_service",
        MagicMock(return_value=fake_cm),
    )

    result = await wuzapi_daemon._try_acquire_port_and_start_service(
        session, port,
    )
    assert result is True
    assert len(session._resources) == 1
    fake_cm.__aenter__.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_try_acquire_returns_false_when_service_start_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Port-bind race: if the probe succeeds but wuzapi_service fails to
    start (another peer grabbed the port in the meantime, or any other
    error), we return False and don't register anything."""
    session = _make_session()
    port = _free_port()

    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(side_effect=OSError("port stolen"))

    monkeypatch.setattr(
        "obscura.integrations.whatsapp.wuzapi.service.wuzapi_service",
        MagicMock(return_value=fake_cm),
    )

    result = await wuzapi_daemon._try_acquire_port_and_start_service(
        session, port,
    )
    assert result is False
    assert len(session._resources) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_promotion_watcher_auto_promotes_when_port_frees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Watcher polls and exits as soon as a probe succeeds.

    Stubs the per-iteration acquire to fail twice then succeed; verifies
    the watcher calls it the expected number of times and returns.
    """
    monkeypatch.setattr(wuzapi_daemon, "_PROMOTION_PROBE_INTERVAL_S", 0.01)

    session = _make_session()
    port = _free_port()

    call_count = 0

    async def fake_acquire(_s: Any, _p: int) -> bool:
        nonlocal call_count
        call_count += 1
        return call_count >= 3

    monkeypatch.setattr(
        wuzapi_daemon,
        "_try_acquire_port_and_start_service",
        fake_acquire,
    )

    await asyncio.wait_for(
        wuzapi_daemon._promotion_watcher(session, port),
        timeout=2.0,
    )
    assert call_count == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_promotion_watcher_survives_probe_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient exception inside one probe must not kill the watcher;
    it should keep polling until a later probe succeeds."""
    monkeypatch.setattr(wuzapi_daemon, "_PROMOTION_PROBE_INTERVAL_S", 0.01)

    session = _make_session()
    port = _free_port()

    call_count = 0

    async def fake_acquire(_s: Any, _p: int) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient network blip")
        return True

    monkeypatch.setattr(
        wuzapi_daemon,
        "_try_acquire_port_and_start_service",
        fake_acquire,
    )

    await asyncio.wait_for(
        wuzapi_daemon._promotion_watcher(session, port),
        timeout=2.0,
    )
    assert call_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_promotion_watcher_clean_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during sleep returns cleanly (no exception leaked)."""
    monkeypatch.setattr(wuzapi_daemon, "_PROMOTION_PROBE_INTERVAL_S", 60.0)

    session = _make_session()
    port = _free_port()

    async def always_fail(_s: Any, _p: int) -> bool:
        return False

    monkeypatch.setattr(
        wuzapi_daemon,
        "_try_acquire_port_and_start_service",
        always_fail,
    )

    task = asyncio.create_task(
        wuzapi_daemon._promotion_watcher(session, port),
    )
    await asyncio.sleep(0.05)
    task.cancel()
    # Should return cleanly without raising
    await task
