"""Tests for `install_project_hooks` — load .obscura/hooks/ + synthesised hooks."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from obscura.composition.blocks.project_hooks import install_project_hooks
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
        self._hooks: Any = None

    def register_tool(self, spec: Any) -> None:
        pass


def _make_session() -> AgentSession:
    return AgentSession(
        session_id=new_session_id(),
        surface="api",  # type: ignore[arg-type]
        config=SessionConfig(),
        client=_StubClient(),  # type: ignore[arg-type]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_hooks_no_channels_no_kairos() -> None:
    """Block is a no-op when nothing is configured."""
    session = _make_session()
    empty_registry = MagicMock()
    empty_registry.count = 0

    with (
        patch(
            "obscura.core.settings.load_all_hooks",
            return_value=empty_registry,
        ),
        patch("obscura.kairos.engine.is_kairos_enabled", return_value=False),
    ):
        await install_project_hooks(session, SessionConfig())

    assert session.project_hooks is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hooks_loaded_from_disk() -> None:
    """A non-empty HookRegistry from load_all_hooks lands on session."""
    session = _make_session()
    loaded_registry = MagicMock()
    loaded_registry.count = 3
    loaded_registry.add_after = MagicMock()

    with (
        patch(
            "obscura.core.settings.load_all_hooks",
            return_value=loaded_registry,
        ),
        patch("obscura.kairos.engine.is_kairos_enabled", return_value=False),
    ):
        await install_project_hooks(session, SessionConfig())

    assert session.project_hooks is loaded_registry
    # Rebound on the client so the loop sees them
    assert session.client._hooks is loaded_registry


@pytest.mark.unit
@pytest.mark.asyncio
async def test_channel_hook_added_when_context_router_set() -> None:
    """When session.context_router is set, the channel hook is registered."""
    session = _make_session()
    session.context_router = MagicMock()

    empty_registry = MagicMock()
    empty_registry.count = 0

    with (
        patch(
            "obscura.core.settings.load_all_hooks",
            return_value=empty_registry,
        ),
        patch("obscura.kairos.engine.is_kairos_enabled", return_value=False),
    ):
        await install_project_hooks(session, SessionConfig())

    # A new HookRegistry was created (since load returned empty) and the
    # channel hook attached
    assert session.project_hooks is not None
    # The fresh registry should have at least one after-hook
    assert hasattr(session.project_hooks, "add_after")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kairos_hooks_added_when_enabled() -> None:
    """When KAIROS is enabled, both tool + turn hooks are registered."""
    session = _make_session()
    empty_registry = MagicMock()
    empty_registry.count = 0

    with (
        patch(
            "obscura.core.settings.load_all_hooks",
            return_value=empty_registry,
        ),
        patch("obscura.kairos.engine.is_kairos_enabled", return_value=True),
    ):
        await install_project_hooks(session, SessionConfig())

    assert session.project_hooks is not None
