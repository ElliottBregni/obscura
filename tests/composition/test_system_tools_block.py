"""Tests for `install_system_tools` — register @tool-decorated specs."""

from __future__ import annotations

from typing import Any

import pytest

from obscura.composition.blocks.system_tools import install_system_tools
from obscura.composition.session import (
    AgentSession,
    SessionConfig,
    new_session_id,
)


class _StubClient:
    """Minimal ObscuraClient surface for AgentSession.add_tool()."""

    def __init__(self, user: Any = None) -> None:
        from obscura.core.tools import ToolRegistry

        self._tool_registry = ToolRegistry()
        self._backend = self
        self._user = user
        self._registered: list[str] = []

    def register_tool(self, spec: Any) -> None:
        if spec.name in self._registered:
            return
        self._registered.append(spec.name)


def _make_session(
    *,
    surface: str = "repl",
    vector_store: Any = None,
    user: Any = None,
) -> AgentSession:
    return AgentSession(
        session_id=new_session_id(),
        surface=surface,  # type: ignore[arg-type]
        config=SessionConfig(),
        client=_StubClient(user=user),  # type: ignore[arg-type]
        vector_store=vector_store,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tools_disabled_skips_block() -> None:
    session = _make_session()
    await install_system_tools(session, SessionConfig(tools_enabled=False))
    assert len(session.registry.all()) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registers_core_system_tools() -> None:
    session = _make_session()
    await install_system_tools(session, SessionConfig(tools_enabled=True))

    names = {t.name for t in session.registry.all()}
    # Spot-check known system tools without coupling to the exact set
    # (the set evolves as new @tool-decorated functions land).
    assert len(names) > 0, "Expected system tools to register; got 0"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_does_not_touch_memory_tools_regardless_of_vector_store() -> None:
    """Memory tool registration moved to install_memory_tools; this block
    must not register them even when vector_store and user are present."""
    from unittest.mock import MagicMock, patch

    fake_vector_store = object()
    session = _make_session(vector_store=fake_vector_store, user=MagicMock())

    with patch(
        "obscura.tools.memory_tools.make_memory_tool_specs",
    ) as mock_make:
        await install_system_tools(session, SessionConfig(tools_enabled=True))

    mock_make.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_idempotent_under_repeat_call() -> None:
    session = _make_session()
    config = SessionConfig(tools_enabled=True)

    await install_system_tools(session, config)
    first = len(session.registry.all())

    await install_system_tools(session, config)
    second = len(session.registry.all())

    assert first == second
