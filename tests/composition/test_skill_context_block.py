"""Tests for `install_skill_context` — OBSCURA.md + skill catalog injection."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from obscura.composition.blocks.skill_context import install_skill_context
from obscura.composition.session import (
    AgentSession,
    SessionConfig,
    new_session_id,
)


class _StubBackend:
    def __init__(self) -> None:
        self._system_prompt = ""

    def register_tool(self, spec: Any) -> None:
        pass


class _StubClient:
    def __init__(self) -> None:
        from obscura.core.tools import ToolRegistry

        self._tool_registry = ToolRegistry()
        self._system_prompt = ""
        self._backend = _StubBackend()

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
async def test_inject_disabled_skipped() -> None:
    session = _make_session()
    config = SessionConfig(
        backend="copilot",
        inject_claude_context=False,
        system_prompt="BASE",
    )
    await install_skill_context(session, config)
    # Backend prompt unchanged
    assert session.client._backend._system_prompt == ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_loader_returns_empty_no_op() -> None:
    """When ContextLoader returns empty string, prompt unchanged."""
    session = _make_session()
    session.system_prompt = "BASE"
    config = SessionConfig(
        backend="copilot",
        inject_claude_context=True,
        system_prompt="BASE",
    )

    fake_loader = MagicMock()
    fake_loader.load_system_prompt = MagicMock(return_value="")

    with patch("obscura.core.context.ContextLoader", return_value=fake_loader):
        await install_skill_context(session, config)

    assert session.client._backend._system_prompt == ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_loader_prepends_to_prompt() -> None:
    """Returned skill context is prepended to the existing system_prompt."""
    session = _make_session()
    session.system_prompt = "BASE_PROMPT"
    config = SessionConfig(
        backend="copilot",
        inject_claude_context=True,
        system_prompt="BASE_PROMPT",
    )

    fake_loader = MagicMock()
    fake_loader.load_system_prompt = MagicMock(return_value="SKILL_CTX")

    with patch("obscura.core.context.ContextLoader", return_value=fake_loader):
        await install_skill_context(session, config)

    expected = "SKILL_CTX\n\nBASE_PROMPT"
    assert session.system_prompt == expected
    assert session.client._backend._system_prompt == expected
    assert session.client._system_prompt == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_base_prompt_uses_skill_only() -> None:
    """When no base prompt is set, just use the skill context."""
    session = _make_session()
    session.system_prompt = ""
    config = SessionConfig(
        backend="copilot",
        inject_claude_context=True,
        system_prompt="",
    )

    fake_loader = MagicMock()
    fake_loader.load_system_prompt = MagicMock(return_value="SKILL_CTX")

    with patch("obscura.core.context.ContextLoader", return_value=fake_loader):
        await install_skill_context(session, config)

    assert session.system_prompt == "SKILL_CTX"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_loader_failure_swallowed() -> None:
    """ContextLoader exceptions don't propagate; prompt unchanged."""
    session = _make_session()
    session.system_prompt = "BASE"
    config = SessionConfig(
        backend="copilot",
        inject_claude_context=True,
        system_prompt="BASE",
    )

    with patch(
        "obscura.core.context.ContextLoader",
        side_effect=RuntimeError("boom"),
    ):
        await install_skill_context(session, config)

    assert session.client._backend._system_prompt == ""
