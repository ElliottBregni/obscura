"""Tests for `install_browser_bridge` — Chrome side-panel attach (REPL only)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obscura.composition.blocks.browser_bridge import install_browser_bridge
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
    """API/A2A surfaces don't try to attach to a local extension."""
    for surface in ("api", "a2a", "mcp_server"):
        session = _make_session(surface=surface)
        await install_browser_bridge(session, SessionConfig(tools_enabled=True))
        assert session.browser_bridge is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tools_disabled_skipped() -> None:
    session = _make_session()
    await install_browser_bridge(session, SessionConfig(tools_enabled=False))
    assert session.browser_bridge is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extension_not_running_silently_skipped() -> None:
    """When attach_if_running returns (None, None), block is a no-op."""
    session = _make_session()
    with patch(
        "obscura.integrations.browser.client.attach_if_running",
        new=AsyncMock(return_value=(None, None)),
    ):
        await install_browser_bridge(session, SessionConfig(tools_enabled=True))
    assert session.browser_bridge is None
    assert len(session.registry.all()) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extension_running_attaches_and_registers_resource() -> None:
    """When the extension is running, the bridge lands on the session and
    registers for teardown."""
    session = _make_session()

    fake_bridge = MagicMock()
    fake_bridge.aclose = AsyncMock()  # has aclose → eligible for teardown
    fake_status = {"ok": True, "tool_count": 27}

    with patch(
        "obscura.integrations.browser.client.attach_if_running",
        new=AsyncMock(return_value=(fake_bridge, fake_status)),
    ):
        await install_browser_bridge(session, SessionConfig(tools_enabled=True))

    assert session.browser_bridge is fake_bridge
    # Resource registered for teardown
    assert len(session._resources) == 1
