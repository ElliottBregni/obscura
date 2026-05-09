"""Tests for `install_kairos_engine` — KAIROS background daemon block."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obscura.composition.blocks.kairos import install_kairos_engine
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
    session = _make_session(surface="api")
    await install_kairos_engine(session, SessionConfig())
    assert session.kairos_engine is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kairos_disabled_skipped() -> None:
    session = _make_session()
    with patch("obscura.kairos.engine.is_kairos_enabled", return_value=False):
        await install_kairos_engine(session, SessionConfig())
    assert session.kairos_engine is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_supervisor_present_registers_hooks_no_start() -> None:
    """When supervisor is set with hooks, register_kairos_hooks runs and
    engine is NOT started directly."""
    fake_supervisor = MagicMock()
    fake_supervisor.hooks = MagicMock()  # has hooks attr
    session = _make_session(supervisor=fake_supervisor)

    fake_engine = MagicMock()
    fake_engine.start = AsyncMock()

    with (
        patch("obscura.kairos.engine.is_kairos_enabled", return_value=True),
        patch("obscura.kairos.engine.KairosEngine", return_value=fake_engine),
        patch(
            "obscura.kairos.supervisor_hooks.register_kairos_hooks",
        ) as register_hooks,
    ):
        await install_kairos_engine(session, SessionConfig())

    register_hooks.assert_called_once()
    fake_engine.start.assert_not_called()
    assert session.kairos_engine is fake_engine


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_supervisor_starts_engine_directly() -> None:
    """When supervisor is None, engine.start() runs and stop is registered."""
    session = _make_session(supervisor=None)
    fake_engine = MagicMock()
    fake_engine.start = AsyncMock()
    fake_engine.stop = MagicMock()  # sync stop

    with (
        patch("obscura.kairos.engine.is_kairos_enabled", return_value=True),
        patch("obscura.kairos.engine.KairosEngine", return_value=fake_engine),
    ):
        await install_kairos_engine(session, SessionConfig())

    fake_engine.start.assert_awaited_once()
    assert session.kairos_engine is fake_engine
    # stop wrapped + registered for teardown
    assert len(session._resources) == 1
