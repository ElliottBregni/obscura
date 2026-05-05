"""Tests for `obscura.composition.blocks.plugins.install_plugin_tools`.

The block is the canonical plugin-loading path for every surface. These
tests cover its contract: tools register exactly once, capability
resolver attaches, opt-out paths work cleanly, and re-running the block
on the same session is a no-op.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from obscura.composition.blocks.plugins import install_plugin_tools
from obscura.composition.session import (
    AgentSession,
    SessionConfig,
    new_session_id,
)
from obscura.core.types import ToolSpec


class _StubClient:
    """Just enough ObscuraClient surface for AgentSession.add_tool()."""

    def __init__(self) -> None:
        from obscura.core.tools import ToolRegistry

        self._tool_registry = ToolRegistry()
        self._backend = self  # backend.register_tool delegates back to us

        self._registered: list[ToolSpec] = []

    def register_tool(self, spec: ToolSpec) -> None:
        if any(t.name == spec.name for t in self._registered):
            return
        self._registered.append(spec)


def _make_session(surface: str = "repl") -> AgentSession:
    return AgentSession(
        session_id=new_session_id(),
        surface=surface,  # type: ignore[arg-type]
        config=SessionConfig(),
        client=_StubClient(),  # type: ignore[arg-type]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tools_disabled_skips_registration() -> None:
    """`config.tools_enabled=False` is the hard opt-out."""
    session = _make_session()
    config = SessionConfig(tools_enabled=False)

    await install_plugin_tools(session, config)

    assert len(session.registry.all()) == 0, (
        "tools_enabled=False must register zero tools"
    )
    assert session.capability_resolver is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_load_builtins_false_skips_registration() -> None:
    """Workspace `plugins.load_builtins=false` is the second opt-out."""
    session = _make_session()
    config = SessionConfig(tools_enabled=True)

    with patch(
        "obscura.plugins.loader._load_plugin_config_flag",
        return_value=False,
    ):
        await install_plugin_tools(session, config)

    assert len(session.registry.all()) == 0
    assert session.capability_resolver is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registers_builtin_tools_with_capability_resolver() -> None:
    """Happy path: block registers builtin tools and exposes resolver."""
    session = _make_session()
    config = SessionConfig(tools_enabled=True)

    await install_plugin_tools(session, config)

    # Real plugin loader returns a non-empty set (provided builtins are
    # discoverable in the test env). If your dev workspace genuinely has
    # zero builtins this is a config issue, not a block bug.
    tool_count = len(session.registry.all())
    assert tool_count > 0, (
        "Expected builtin plugin tools to register; got 0. Check that "
        "`obscura.plugins.builtins.list_builtin_manifests()` returns "
        "specs in the test environment."
    )
    assert session.capability_resolver is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_idempotent_under_repeat_call() -> None:
    """Running the block twice doesn't double-register tools."""
    session = _make_session()
    config = SessionConfig(tools_enabled=True)

    await install_plugin_tools(session, config)
    first_count = len(session.registry.all())

    await install_plugin_tools(session, config)
    second_count = len(session.registry.all())

    assert first_count == second_count, (
        "Repeat call must be a no-op (idempotent contract)"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_capability_resolver_exposes_index() -> None:
    """The resolver must expose its capability_index property so the
    composition tool-router can read it without rebuilding from scratch.
    """
    session = _make_session()
    config = SessionConfig(tools_enabled=True)

    await install_plugin_tools(session, config)

    resolver = session.capability_resolver
    if resolver is None:
        pytest.skip("Capability resolver build was skipped (env config)")
    # The new public property added in this refactor:
    assert resolver.capability_index is not None
    assert resolver.tool_index is not None
