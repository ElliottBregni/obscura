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
from unittest.mock import MagicMock, patch

import pytest

from obscura.composition.session import SessionConfig


class _RecordingBackend:
    """Stand-in backend that records tool registrations."""

    def __init__(self) -> None:
        self._registered: list[str] = []
        self._system_prompt = ""

    def register_tool(self, spec: Any) -> None:
        if spec.name not in self._registered:
            self._registered.append(spec.name)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


@pytest.fixture
def patched_client():
    """Patch composition.core's backend factory so build_core_session
    doesn't try to instantiate a real LLM backend.

    Stage 4b: composition no longer goes through ObscuraClient — it
    builds the backend via create_backend. Patch THAT, plus
    resolve_auth (returns AuthConfig).
    """
    with (
        patch(
            "obscura.composition.backend_factory.create_backend",
            side_effect=lambda **_kw: _RecordingBackend(),
        ) as m,
        patch(
            "obscura.core.auth.resolve_auth",
            return_value=MagicMock(),
        ),
    ):
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

    # browser_* tools are REPL-only (Chrome side-panel doesn't reach API/A2A).
    # Every other tool must be identical across all surfaces.
    def _comparable(names: set[str]) -> set[str]:
        return {n for n in names if not n.startswith("browser_")}

    repl_tools = _comparable({t.name for t in repl_session.registry.all()})
    api_tools = _comparable({t.name for t in api_session.registry.all()})
    a2a_tools = _comparable({t.name for t in a2a_session.registry.all()})

    assert repl_tools == api_tools == a2a_tools, (
        "All three surfaces must register the same non-browser tool set.\n"
        f"  REPL only: {repl_tools - api_tools - a2a_tools}\n"
        f"  API only:  {api_tools - repl_tools - a2a_tools}\n"
        f"  A2A only:  {a2a_tools - repl_tools - api_tools}"
    )
    # If we register zero tools the test isn't meaningful; sanity check
    assert len(repl_tools) > 0, (
        "Expected builtin plugin + system tools to register on every "
        "surface; got 0 — check that fixtures discoverable in test env."
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

    backend_tools = session.backend._registered
    registry_tools = {t.name for t in session.registry.all()}

    assert set(backend_tools) == registry_tools, (
        "Tools registered on the registry must also be registered on "
        "the backend so the LLM sees them in tool-use prompts. "
        f"\n  Backend has: {set(backend_tools) - registry_tools}\n"
        f"  Registry has: {registry_tools - set(backend_tools)}"
    )
    assert len(registry_tools) > 0, "A2A must have plugin tools available"
