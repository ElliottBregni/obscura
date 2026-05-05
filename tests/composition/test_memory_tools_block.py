"""Tests for `install_memory_tools` — register memory tool specs."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from obscura.composition.blocks.memory_tools import install_memory_tools
from obscura.composition.session import (
    AgentSession,
    SessionConfig,
    new_session_id,
)
from obscura.core.types import ToolSpec


class _StubClient:
    """Minimal ObscuraClient surface for AgentSession.add_tool()."""

    def __init__(self, user: Any = None) -> None:
        from obscura.core.tools import ToolRegistry

        self._tool_registry = ToolRegistry()
        self._backend = self
        self._user = user
        self._registered: list[str] = []

    def register_tool(self, spec: ToolSpec) -> None:
        if spec.name in self._registered:
            return
        self._registered.append(spec.name)


def _make_session(
    *,
    vector_store: Any = None,
    user: Any = None,
) -> AgentSession:
    return AgentSession(
        session_id=new_session_id(),
        surface="repl",
        config=SessionConfig(),
        client=_StubClient(user=user),  # type: ignore[arg-type]
        vector_store=vector_store,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tools_disabled_skips_block() -> None:
    session = _make_session(vector_store=object(), user=MagicMock())
    with patch(
        "obscura.tools.memory_tools.make_memory_tool_specs",
    ) as mock_make:
        await install_memory_tools(session, SessionConfig(tools_enabled=False))
    mock_make.assert_not_called()
    assert len(session.registry.all()) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_vector_store_skips_block() -> None:
    """Without a vector store, memory tools must not register (the
    surface either opted out of vector memory or no Qdrant is wired)."""
    session = _make_session(vector_store=None, user=MagicMock())
    with patch(
        "obscura.tools.memory_tools.make_memory_tool_specs",
    ) as mock_make:
        await install_memory_tools(session, SessionConfig(tools_enabled=True))
    mock_make.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_user_skips_block() -> None:
    """Vector store present but no authenticated user → skip (no
    namespace to bind memory to)."""
    session = _make_session(vector_store=object(), user=None)
    with patch(
        "obscura.tools.memory_tools.make_memory_tool_specs",
    ) as mock_make:
        await install_memory_tools(session, SessionConfig(tools_enabled=True))
    mock_make.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registers_when_vector_store_and_user_present() -> None:
    fake_vector_store = object()
    session = _make_session(vector_store=fake_vector_store, user=MagicMock())

    fake_memory_tool = ToolSpec(
        name="fake_memory_tool",
        description="...",
        parameters={"type": "object", "properties": {}},
        handler=lambda _args: None,
    )
    with patch(
        "obscura.tools.memory_tools.make_memory_tool_specs",
        return_value=[fake_memory_tool],
    ) as mock_make:
        await install_memory_tools(session, SessionConfig(tools_enabled=True))

    mock_make.assert_called_once()
    assert "fake_memory_tool" in {t.name for t in session.registry.all()}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_idempotent_under_repeat_call() -> None:
    fake_vector_store = object()
    session = _make_session(vector_store=fake_vector_store, user=MagicMock())

    fake_memory_tool = ToolSpec(
        name="fake_memory_tool",
        description="...",
        parameters={"type": "object", "properties": {}},
        handler=lambda _args: None,
    )
    config = SessionConfig(tools_enabled=True)
    with patch(
        "obscura.tools.memory_tools.make_memory_tool_specs",
        return_value=[fake_memory_tool],
    ):
        await install_memory_tools(session, config)
        first = len(session.registry.all())
        await install_memory_tools(session, config)
        second = len(session.registry.all())

    assert first == second == 1
