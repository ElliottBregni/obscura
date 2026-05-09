"""Tests for `install_supervisor` — REPL multi-agent supervisor block."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from obscura.composition.blocks.supervisor import install_supervisor
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
        await install_supervisor(session, SessionConfig(tools_enabled=True))
        assert session.supervisor is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_supervise_false_skipped() -> None:
    session = _make_session()
    config = SessionConfig(tools_enabled=True, extras={"supervise": False})
    await install_supervisor(session, config)
    assert session.supervisor is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_agent_infos_skipped() -> None:
    session = _make_session()
    config = SessionConfig(
        tools_enabled=True,
        extras={"supervise": True, "agent_infos": []},
    )
    await install_supervisor(session, config)
    assert session.supervisor is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_construction_failure_swallowed() -> None:
    """Supervisor init that raises must NOT propagate; fields stay None."""
    session = _make_session()
    config = SessionConfig(
        tools_enabled=True,
        extras={"supervise": True, "agent_infos": [MagicMock()]},
    )
    with patch(
        "obscura.agent.supervisor.AgentSupervisor",
        side_effect=RuntimeError("boom"),
    ):
        await install_supervisor(session, config)

    assert session.supervisor is None
    assert session.supervisor_task is None
