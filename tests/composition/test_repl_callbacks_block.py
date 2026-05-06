"""Tests for `install_repl_callbacks` — REPL TUI widget bridge."""

from __future__ import annotations

from typing import Any

import pytest

from obscura.composition.blocks.repl_callbacks import install_repl_callbacks
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
        await install_repl_callbacks(session, SessionConfig(tools_enabled=True))
        assert "ask_user_callback" not in session.host_callbacks


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tools_disabled_skipped() -> None:
    session = _make_session()
    await install_repl_callbacks(session, SessionConfig(tools_enabled=False))
    assert "ask_user_callback" not in session.host_callbacks


@pytest.mark.unit
@pytest.mark.asyncio
async def test_happy_path_wires_three_callbacks() -> None:
    """ask_user, plan_approval, and user_interact land on host_callbacks."""
    session = _make_session()
    await install_repl_callbacks(session, SessionConfig(tools_enabled=True))

    # All three callbacks set on session.host_callbacks
    assert "ask_user_callback" in session.host_callbacks
    assert "plan_approval_callback" in session.host_callbacks
    assert "user_interact_callback" in session.host_callbacks

    # permission_mode is INTENTIONALLY left to REPL inline (REPLContext
    # coupling). Block should NOT set it.
    assert "permission_mode_callback" not in session.host_callbacks
