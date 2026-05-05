"""Tests for `install_tool_router` — eval-driven tool router."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from obscura.composition.blocks.tool_router import install_tool_router
from obscura.composition.session import (
    AgentSession,
    SessionConfig,
    new_session_id,
)


class _StubBackend:
    """Backend stub that records set_tool_router calls."""

    def __init__(self, capable: bool = True) -> None:
        self._capable = capable
        self.tool_router_set: Any = None

    def set_tool_router(self, router: Any) -> None:
        self.tool_router_set = router


class _RouterCapableStubBackend(_StubBackend):
    """ToolRouterCapable stub: implements set_tool_router."""


class _PlainStubBackend:
    """Backend with NO set_tool_router method (not ToolRouterCapable)."""


class _StubClient:
    def __init__(self, backend: Any) -> None:
        from obscura.core.tools import ToolRegistry

        self._tool_registry = ToolRegistry()
        self._backend = backend

    def register_tool(self, spec: Any) -> None:
        pass


def _make_session(*, backend: Any, resolver: Any = None) -> AgentSession:
    return AgentSession(
        session_id=new_session_id(),
        surface="repl",  # type: ignore[arg-type]
        config=SessionConfig(),
        client=_StubClient(backend=backend),  # type: ignore[arg-type]
        capability_resolver=resolver,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tools_disabled_skips() -> None:
    backend = _RouterCapableStubBackend()
    session = _make_session(backend=backend)
    await install_tool_router(session, SessionConfig(tools_enabled=False))
    assert session.tool_router is None
    assert backend.tool_router_set is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_capability_resolver_skips() -> None:
    backend = _RouterCapableStubBackend()
    session = _make_session(backend=backend, resolver=None)
    await install_tool_router(session, SessionConfig(tools_enabled=True))
    assert session.tool_router is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backend_not_router_capable_skips() -> None:
    backend = _PlainStubBackend()  # no set_tool_router method
    fake_resolver = MagicMock()
    fake_resolver.capability_index = MagicMock()
    session = _make_session(backend=backend, resolver=fake_resolver)
    await install_tool_router(session, SessionConfig(tools_enabled=True))
    assert session.tool_router is None
