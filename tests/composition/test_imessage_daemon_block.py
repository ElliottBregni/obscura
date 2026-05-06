"""Tests for `install_imessage_daemon` — iMessage daemon (REPL only)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obscura.composition.blocks.imessage_daemon import install_imessage_daemon
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


def _make_session(*, surface: str = "repl", supervisor: Any = None) -> AgentSession:
    return AgentSession(
        session_id=new_session_id(),
        surface=surface,  # type: ignore[arg-type]
        config=SessionConfig(),
        client=_StubClient(),  # type: ignore[arg-type]
        supervisor=supervisor,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_repl_surface_skipped() -> None:
    for surface in ("api", "a2a"):
        session = _make_session(surface=surface)
        await install_imessage_daemon(session, SessionConfig())
        assert session.imessage_daemon_task is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_supervisor_present_skipped() -> None:
    """When supervisor is running, this block is a no-op."""
    fake_supervisor = MagicMock()
    session = _make_session(supervisor=fake_supervisor)
    with patch(
        "obscura.cli._daemon.start_imessage_daemon",
        new=AsyncMock(),
    ) as mock_start:
        await install_imessage_daemon(session, SessionConfig())
    mock_start.assert_not_called()
    assert session.imessage_daemon_task is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_returns_none_skipped() -> None:
    """When start_imessage_daemon returns None (no daemon configured),
    block leaves field None."""
    session = _make_session()
    with patch(
        "obscura.cli._daemon.start_imessage_daemon",
        new=AsyncMock(return_value=None),
    ):
        await install_imessage_daemon(session, SessionConfig())
    assert session.imessage_daemon_task is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_happy_path_registers_task() -> None:
    session = _make_session()

    async def _stub_coro() -> None:
        await asyncio.sleep(0)

    fake_task = asyncio.create_task(_stub_coro())
    with patch(
        "obscura.cli._daemon.start_imessage_daemon",
        new=AsyncMock(return_value=fake_task),
    ):
        await install_imessage_daemon(session, SessionConfig())

    assert session.imessage_daemon_task is fake_task
    # Task registered for cancellation
    assert len(session._resources) == 1
    # Cleanup
    fake_task.cancel()
    try:
        await fake_task
    except asyncio.CancelledError:
        pass


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_failure_swallowed() -> None:
    session = _make_session()
    with patch(
        "obscura.cli._daemon.start_imessage_daemon",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await install_imessage_daemon(session, SessionConfig())
    assert session.imessage_daemon_task is None
