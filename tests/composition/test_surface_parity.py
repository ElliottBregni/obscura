"""Surface parity: REPL, API, A2A all see the same plugin tool set.

This is the behavioural counterpart to test_no_drift.py. Even if no
legacy callsites linger, parity could still drift if a surface's
boot pipeline silently dropped a tool. This test boots a minimal
session for each surface and asserts the registered tool names match.

The test mocks ObscuraClient construction to avoid network/auth and
captures what gets registered.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obscura.composition.session import SessionConfig


class _RecordingClient:
    """Stand-in for ObscuraClient that records tool registrations."""

    def __init__(self, *_args: Any, **kwargs: Any) -> None:
        from obscura.core.tools import ToolRegistry

        self._tool_registry = ToolRegistry()
        self._backend = self
        self._registered: list[str] = []
        # Pre-register any tools passed via the `tools` kwarg
        for spec in kwargs.get("tools") or []:
            if spec.name not in self._registered:
                self._registered.append(spec.name)
                self._tool_registry.register(spec)

    def register_tool(self, spec: Any) -> None:
        if spec.name not in self._registered:
            self._registered.append(spec.name)

    async def start(self) -> None:
        return None

    async def __aenter__(self) -> _RecordingClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


@pytest.fixture
def patched_client():
    """Patch ObscuraClient where build_core_session imports it."""
    with patch(
        "obscura.core.client.ObscuraClient",
        new=_RecordingClient,
    ) as m:
        yield m


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repl_api_a2a_register_same_plugin_tools(
    patched_client: Any,  # noqa: ARG001
) -> None:
    """All three surface boot pipelines must register the same plugin tools."""
    from obscura.composition.a2a import build_a2a_session
    from obscura.composition.api import build_api_session
    from obscura.composition.repl import build_repl_session

    config = SessionConfig(backend="copilot", tools_enabled=True)

    # Mock authenticated user for the API surface
    fake_user = MagicMock()
    fake_user.user_id = "test-user"

    repl_session = await build_repl_session(config)
    api_session = await build_api_session(config, user=fake_user)
    a2a_session = await build_a2a_session(config, task_id="t-test")

    repl_tools = {t.name for t in repl_session.registry.all()}
    api_tools = {t.name for t in api_session.registry.all()}
    a2a_tools = {t.name for t in a2a_session.registry.all()}

    assert repl_tools == api_tools == a2a_tools, (
        "All three surfaces must register the same plugin tool set.\n"
        f"  REPL only: {repl_tools - api_tools - a2a_tools}\n"
        f"  API only:  {api_tools - repl_tools - a2a_tools}\n"
        f"  A2A only:  {a2a_tools - repl_tools - api_tools}"
    )
    # If we register zero tools the test isn't meaningful; sanity check
    assert len(repl_tools) > 0, (
        "Expected builtin plugin tools to register on every surface; "
        "got 0 — check that builtin manifests are discoverable in test env."
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a2a_session_registers_tools_on_backend(
    patched_client: Any,  # noqa: ARG001
) -> None:
    """A2A specifically: tools must reach the backend, not just the registry.

    This is the user's load-bearing requirement — A2A agents can call
    tools because the backend sees them in tool-use prompts. Previously
    A2A boot left the backend empty (get_runtime=None path returned
    placeholders), so this test guards against regression.
    """
    from obscura.composition.a2a import build_a2a_session

    config = SessionConfig(backend="copilot", tools_enabled=True)
    session = await build_a2a_session(config, task_id="t-a2a")

    backend_tools = session.client._backend._registered  # type: ignore[union-attr]
    registry_tools = {t.name for t in session.registry.all()}

    assert set(backend_tools) == registry_tools, (
        "Tools registered on the registry must also be registered on "
        "the backend so the LLM sees them in tool-use prompts. "
        f"\n  Backend has: {set(backend_tools) - registry_tools}\n"
        f"  Registry has: {registry_tools - set(backend_tools)}"
    )
    assert len(registry_tools) > 0, "A2A must have plugin tools available"
