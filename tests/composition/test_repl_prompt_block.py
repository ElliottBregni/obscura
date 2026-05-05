"""Tests for `install_repl_prompt_sections` — REPL system prompt enrichment."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from obscura.composition.blocks.repl_prompt import install_repl_prompt_sections
from obscura.composition.session import (
    AgentSession,
    SessionConfig,
    new_session_id,
)


class _StubBackend:
    def __init__(self, system_prompt: str = "") -> None:
        self._system_prompt = system_prompt

    def register_tool(self, spec: Any) -> None:
        pass


class _StubClient:
    def __init__(self, system_prompt: str = "") -> None:
        from obscura.core.tools import ToolRegistry

        self._tool_registry = ToolRegistry()
        self._system_prompt = system_prompt
        self._backend = _StubBackend(system_prompt=system_prompt)

    def register_tool(self, spec: Any) -> None:
        pass


def _make_session(*, surface: str = "repl") -> AgentSession:
    return AgentSession(
        session_id=new_session_id(),
        surface=surface,  # type: ignore[arg-type]
        config=SessionConfig(system_prompt="BASE_PROMPT"),
        client=_StubClient(),  # type: ignore[arg-type]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_repl_surface_skipped() -> None:
    for surface in ("api", "a2a"):
        session = _make_session(surface=surface)
        await install_repl_prompt_sections(
            session,
            SessionConfig(system_prompt="BASE_PROMPT"),
        )
        # Backend prompt unchanged — block opted out
        assert session.client._backend._system_prompt == ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_happy_path_mutates_backend_prompt() -> None:
    """Block composes a prompt and re-primes the backend via update_system_prompt."""
    session = _make_session()
    config = SessionConfig(system_prompt="BASE")

    # Mock all the section sources to return predictable bits
    with (
        patch(
            "obscura.core.context.load_obscura_memory",
            return_value="MEM_CTX",
        ),
        patch("obscura.kairos.engine.is_kairos_enabled", return_value=False),
        patch("obscura.agent.coordinator.is_coordinator_mode", return_value=False),
        patch(
            "obscura.cli.vector_memory_bridge.load_startup_memories",
        ),
        patch(
            "obscura.tools.memory_tools.build_channels_prompt_section",
        ),
        patch(
            "obscura.core.system_prompts.compose_environment_context",
            return_value="ENV",
        ),
        patch(
            "obscura.core.system_prompts.compose_system_prompt",
            return_value="COMPOSED_PROMPT",
        ),
    ):
        await install_repl_prompt_sections(session, config)

    # Backend's _system_prompt was mutated to the composed result
    assert session.client._backend._system_prompt == "COMPOSED_PROMPT"
    assert session.system_prompt == "COMPOSED_PROMPT"
    assert session.client._system_prompt == "COMPOSED_PROMPT"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handles_no_vector_store() -> None:
    """When session.vector_store is None, prompt composition still works
    without the startup-memory section."""
    session = _make_session()
    session.vector_store = None
    config = SessionConfig(system_prompt="BASE")

    fake_load = MagicMock(return_value="")
    with (
        patch(
            "obscura.core.context.load_obscura_memory",
            return_value="",
        ),
        patch("obscura.kairos.engine.is_kairos_enabled", return_value=False),
        patch("obscura.agent.coordinator.is_coordinator_mode", return_value=False),
        patch(
            "obscura.core.system_prompts.compose_environment_context",
            return_value="",
        ),
        patch(
            "obscura.core.system_prompts.compose_system_prompt",
            return_value="OUT",
        ),
        patch(
            "obscura.cli.vector_memory_bridge.load_startup_memories",
            new=fake_load,
        ),
    ):
        await install_repl_prompt_sections(session, config)

    fake_load.assert_not_called()
