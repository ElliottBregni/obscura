"""Tests for AgentSession's client-forwarding methods.

`session.send`, `session.stream`, `session.resume_session`,
`session.delete_session`, `session.create_backend_session`, and
`session.capability_tier` are thin wrappers over the underlying
`ObscuraClient`. These tests verify the wrappers forward args through
unchanged. Step 1 of the eventual ObscuraClient absorption.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.composition.session import (
    AgentSession,
    SessionConfig,
    new_session_id,
)


class _ForwardingClient:
    """Records every method call for assertion."""

    def __init__(self) -> None:
        from obscura.core.tools import ToolRegistry

        self._tool_registry = ToolRegistry()
        self._backend = self
        self.send = AsyncMock(return_value="SEND_RESULT")
        self.resume_session = AsyncMock()
        self.delete_session = AsyncMock()
        self.create_session = AsyncMock(return_value="CREATED_REF")
        self.capability_tier = "privileged"

    def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[str]:  # noqa: ARG002
        async def _gen() -> AsyncIterator[str]:
            yield "chunk1"
            yield "chunk2"

        return _gen()

    def register_tool(self, spec: Any) -> None:
        pass


def _make_session() -> AgentSession:
    return AgentSession(
        session_id=new_session_id(),
        surface="api",  # type: ignore[arg-type]
        config=SessionConfig(),
        client=_ForwardingClient(),  # type: ignore[arg-type]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_forwards_args() -> None:
    session = _make_session()
    result = await session.send("hello", mode="x")
    assert result == "SEND_RESULT"
    session.client.send.assert_awaited_once_with("hello", mode="x")  # type: ignore[attr-defined]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_forwards() -> None:
    session = _make_session()
    chunks = []
    async for c in session.stream("hello"):
        chunks.append(c)
    assert chunks == ["chunk1", "chunk2"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_session_forwards() -> None:
    session = _make_session()
    fake_ref = MagicMock()
    await session.resume_session(fake_ref)
    session.client.resume_session.assert_awaited_once_with(fake_ref)  # type: ignore[attr-defined]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_session_forwards() -> None:
    session = _make_session()
    fake_ref = MagicMock()
    await session.delete_session(fake_ref)
    session.client.delete_session.assert_awaited_once_with(fake_ref)  # type: ignore[attr-defined]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_backend_session_forwards() -> None:
    session = _make_session()
    ref = await session.create_backend_session()
    assert ref == "CREATED_REF"


@pytest.mark.unit
def test_capability_tier_forwards() -> None:
    session = _make_session()
    assert session.capability_tier == "privileged"


@pytest.mark.unit
def test_capability_tier_handles_none() -> None:
    """If client.capability_tier is None, session returns empty string."""
    session = _make_session()
    session.client.capability_tier = None  # type: ignore[attr-defined]
    assert session.capability_tier == ""
