"""Tests for `install_vector_memory` — vector store + channel router init."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from obscura.composition.blocks.vector_memory import install_vector_memory
from obscura.composition.session import (
    AgentSession,
    SessionConfig,
    new_session_id,
)


class _StubClient:
    def __init__(self, user: Any = None) -> None:
        from obscura.core.tools import ToolRegistry

        self._tool_registry = ToolRegistry()
        self._backend = self
        self._user = user

    def register_tool(self, spec: Any) -> None:
        pass


def _make_session(*, surface: str = "api", user: Any = None) -> AgentSession:
    return AgentSession(
        session_id=new_session_id(),
        surface=surface,  # type: ignore[arg-type]
        config=SessionConfig(),
        client=_StubClient(user=user),  # type: ignore[arg-type]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tools_disabled_skips() -> None:
    session = _make_session(user=MagicMock())
    await install_vector_memory(session, SessionConfig(tools_enabled=False))
    assert session.vector_store is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_env_off_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _make_session(user=MagicMock())
    monkeypatch.setenv("OBSCURA_VECTOR_MEMORY", "off")
    await install_vector_memory(session, SessionConfig(tools_enabled=True))
    assert session.vector_store is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_user_skips() -> None:
    session = _make_session(user=None)
    await install_vector_memory(session, SessionConfig(tools_enabled=True))
    assert session.vector_store is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_init_returns_none_skips() -> None:
    """When init_vector_store returns None (no Qdrant), block opts out."""
    session = _make_session(user=MagicMock())
    with patch(
        "obscura.cli.vector_memory_bridge.init_vector_store",
        return_value=None,
    ):
        await install_vector_memory(session, SessionConfig(tools_enabled=True))
    assert session.vector_store is None
    assert session.context_router is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_happy_path_sets_vector_store() -> None:
    """init_vector_store returns a store → it lands on session."""
    session = _make_session(user=MagicMock())
    fake_store = MagicMock()
    fake_store.aclose = MagicMock()  # truthy hasattr triggers register_resource

    with (
        patch(
            "obscura.cli.vector_memory_bridge.init_vector_store",
            return_value=fake_store,
        ),
        patch("obscura.cli.vector_memory_bridge.run_startup_maintenance"),
        patch(
            "obscura.memory_channels.load_channels_from_config",
            return_value=[],
        ),
    ):
        await install_vector_memory(session, SessionConfig(tools_enabled=True))

    assert session.vector_store is fake_store
    assert session.context_router is None  # no channels configured


@pytest.mark.unit
@pytest.mark.asyncio
async def test_channels_set_context_router() -> None:
    """When channels.yaml has entries, context_router + turn_classifier set."""
    session = _make_session(user=MagicMock())
    fake_store = MagicMock(spec=[])  # no aclose, no close

    with (
        patch(
            "obscura.cli.vector_memory_bridge.init_vector_store",
            return_value=fake_store,
        ),
        patch("obscura.cli.vector_memory_bridge.run_startup_maintenance"),
        patch(
            "obscura.memory_channels.load_channels_from_config",
            return_value=[MagicMock()],
        ),
    ):
        await install_vector_memory(session, SessionConfig(tools_enabled=True))

    assert session.vector_store is fake_store
    assert session.context_router is not None
    assert session.turn_classifier is not None
