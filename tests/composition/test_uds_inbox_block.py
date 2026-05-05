"""Tests for `install_uds_inbox` — cross-session UDS messaging."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obscura.composition.blocks.uds_inbox import install_uds_inbox
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


def _make_session(*, surface: str = "repl") -> AgentSession:
    return AgentSession(
        session_id=new_session_id(),
        surface=surface,  # type: ignore[arg-type]
        config=SessionConfig(),
        client=_StubClient(),  # type: ignore[arg-type]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_repl_surface_skipped() -> None:
    for surface in ("api", "a2a"):
        session = _make_session(surface=surface)
        await install_uds_inbox(session, SessionConfig(tools_enabled=True))
        assert session.uds_inbox is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tools_disabled_skipped() -> None:
    session = _make_session()
    await install_uds_inbox(session, SessionConfig(tools_enabled=False))
    assert session.uds_inbox is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_happy_path() -> None:
    session = _make_session()
    fake_inbox = MagicMock()
    fake_inbox.start = AsyncMock()
    fake_inbox.stop = MagicMock()

    with patch(
        "obscura.kairos.uds_messaging.UDSInbox",
        return_value=fake_inbox,
    ):
        await install_uds_inbox(session, SessionConfig(tools_enabled=True))

    assert session.uds_inbox is fake_inbox
    fake_inbox.start.assert_awaited_once()
    # Stop registered for teardown
    assert len(session._resources) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_construction_failure_swallowed() -> None:
    session = _make_session()
    with patch(
        "obscura.kairos.uds_messaging.UDSInbox",
        side_effect=RuntimeError("boom"),
    ):
        await install_uds_inbox(session, SessionConfig(tools_enabled=True))
    assert session.uds_inbox is None
