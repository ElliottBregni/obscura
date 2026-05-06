"""Tests for `install_session_registration` — PID lock + signal handlers."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from obscura.composition.blocks.session_registration import (
    install_session_registration,
)
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
        config=SessionConfig(backend="copilot"),
        client=_StubClient(),  # type: ignore[arg-type]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_repl_surface_skipped() -> None:
    for surface in ("api", "a2a"):
        session = _make_session(surface=surface)
        with patch(
            "obscura.core.session_utils.register_session",
        ) as mock_reg:
            await install_session_registration(session, SessionConfig())
        mock_reg.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_happy_path_registers_and_installs_handlers() -> None:
    session = _make_session()
    with (
        patch(
            "obscura.core.session_utils.register_session",
        ) as mock_reg,
        patch(
            "obscura.core.session_utils.install_signal_handlers",
        ) as mock_install,
        patch(
            "obscura.core.session_utils.register_shutdown_handler",
        ) as mock_shut,
        patch(
            "obscura.core.session_utils.check_concurrent_sessions",
            return_value=[],
        ),
    ):
        await install_session_registration(session, SessionConfig())

    mock_reg.assert_called_once()
    mock_install.assert_called_once()
    mock_shut.assert_called_once()
    # unregister callback registered for teardown
    assert len(session._resources) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_register_failure_swallowed() -> None:
    """If register_session raises, the block should NOT propagate."""
    session = _make_session()
    with (
        patch(
            "obscura.core.session_utils.register_session",
            side_effect=RuntimeError("boom"),
        ),
        patch("obscura.core.session_utils.install_signal_handlers"),
        patch("obscura.core.session_utils.register_shutdown_handler"),
        patch(
            "obscura.core.session_utils.check_concurrent_sessions",
            return_value=[],
        ),
    ):
        await install_session_registration(session, SessionConfig())
