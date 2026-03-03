"""Tests for the delegation tool and tool allowlist enforcement."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from obscura.core.agent_loop import AgentLoop
from obscura.core.event_store import SQLiteEventStore
from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    AgentEvent,
    AgentEventKind,
    Backend,
    BackendCapabilities,
    ChunkKind,
    HookPoint,
    Message,
    NativeHandle,
    Role,
    SessionRef,
    StreamChunk,
    ToolSpec,
)
from obscura.tools.delegation import DelegationContext, make_task_tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_text_chunks(text: str) -> list[StreamChunk]:
    chunks: list[StreamChunk] = []
    for word in text.split(" "):
        chunks.append(StreamChunk(kind=ChunkKind.TEXT_DELTA, text=word + " "))
    chunks.append(StreamChunk(kind=ChunkKind.DONE))
    return chunks


class MockBackend:
    """Deterministic backend for tests."""

    def __init__(self, turn_responses: list[list[StreamChunk]]) -> None:
        self._turns = list(turn_responses)
        self._call_count = 0
        self._registry = ToolRegistry()

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        if self._call_count < len(self._turns):
            chunks = self._turns[self._call_count]
        else:
            chunks = [StreamChunk(kind=ChunkKind.DONE)]
        self._call_count += 1
        for chunk in chunks:
            yield chunk

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        return Message(role=Role.ASSISTANT, content=[], raw=None)

    async def create_session(self, **kwargs: Any) -> SessionRef:
        return SessionRef(session_id="s", backend=Backend.COPILOT)

    async def resume_session(self, ref: SessionRef) -> None:
        return None

    async def list_sessions(self) -> list[SessionRef]:
        return []

    async def delete_session(self, ref: SessionRef) -> None:
        return None

    def register_tool(self, spec: ToolSpec) -> None:
        self._registry.register(spec)

    def register_hook(self, hook: HookPoint, callback: Any) -> None:
        return None

    def get_tool_registry(self) -> ToolRegistry:
        return self._registry

    @property
    def native(self) -> NativeHandle:
        return NativeHandle()

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()


def _make_registry(*specs: ToolSpec) -> ToolRegistry:
    reg = ToolRegistry()
    for s in specs:
        reg.register(s)
    return reg


# ---------------------------------------------------------------------------
# Delegation tool tests
# ---------------------------------------------------------------------------


class TestDelegationTool:
    @pytest.mark.asyncio
    async def test_delegation_disabled(self) -> None:
        """Tool returns error when can_delegate=False."""
        ctx = DelegationContext(can_delegate=False)
        tool = make_task_tool(ctx)
        result = json.loads(await tool.handler(prompt="do something"))
        assert result["ok"] is False
        assert result["error"] == "delegation_disabled"

    @pytest.mark.asyncio
    async def test_max_depth_exceeded(self) -> None:
        """Tool returns error when depth limit reached."""
        ctx = DelegationContext(
            can_delegate=True,
            max_delegation_depth=2,
            current_depth=2,
        )
        tool = make_task_tool(ctx)
        result = json.loads(await tool.handler(prompt="do something"))
        assert result["ok"] is False
        assert result["error"] == "max_depth_exceeded"

    @pytest.mark.asyncio
    async def test_target_not_in_allowlist(self) -> None:
        """Tool returns error when target not in allowlist."""
        ctx = DelegationContext(
            can_delegate=True,
            delegate_allowlist=["researcher", "code-reviewer"],
        )
        tool = make_task_tool(ctx)
        result = json.loads(
            await tool.handler(prompt="do something", target="hacker")
        )
        assert result["ok"] is False
        assert result["error"] == "target_not_allowed"

    @pytest.mark.asyncio
    async def test_empty_allowlist_allows_all(self) -> None:
        """Empty allowlist means all targets are permitted (no peer registry → falls through)."""
        ctx = DelegationContext(
            can_delegate=True,
            delegate_allowlist=[],
        )
        tool = make_task_tool(ctx)
        # No peer registry → will fail at "no_peer_registry", not allowlist
        result = json.loads(
            await tool.handler(prompt="do something", target="anyone")
        )
        assert result["error"] == "no_peer_registry"

    @pytest.mark.asyncio
    async def test_no_peer_registry(self) -> None:
        """Tool returns error when no peer registry is set."""
        ctx = DelegationContext(
            can_delegate=True,
            peer_registry=None,
        )
        tool = make_task_tool(ctx)
        result = json.loads(
            await tool.handler(prompt="do something", target="agent")
        )
        assert result["ok"] is False
        assert result["error"] == "no_peer_registry"

    @pytest.mark.asyncio
    async def test_target_not_found(self) -> None:
        """Tool returns error when target agent doesn't exist."""
        from unittest.mock import MagicMock

        mock_registry = MagicMock()
        mock_registry.resolve.return_value = None
        mock_registry.discover.return_value = []

        ctx = DelegationContext(
            can_delegate=True,
            peer_registry=mock_registry,
        )
        tool = make_task_tool(ctx)
        result = json.loads(
            await tool.handler(prompt="do something", target="ghost")
        )
        assert result["ok"] is False
        assert result["error"] == "target_not_found"

    @pytest.mark.asyncio
    async def test_successful_delegation(self) -> None:
        """Successful delegation returns ok=True with result."""
        from unittest.mock import AsyncMock, MagicMock

        mock_agent = MagicMock()
        mock_agent.run_loop = AsyncMock(return_value="delegation result text")

        mock_registry = MagicMock()
        mock_registry.resolve.return_value = mock_agent
        mock_registry.discover.return_value = []

        ctx = DelegationContext(
            can_delegate=True,
            peer_registry=mock_registry,
        )
        tool = make_task_tool(ctx)
        result = json.loads(
            await tool.handler(prompt="analyze this", target="researcher")
        )
        assert result["ok"] is True
        assert result["result"] == "delegation result text"
        assert result["target"] == "researcher"
        mock_agent.run_loop.assert_awaited_once_with("analyze this")

    @pytest.mark.asyncio
    async def test_delegation_creates_child_session(
        self, tmp_path: Path
    ) -> None:
        """Delegation creates a child session in the event store."""
        from unittest.mock import AsyncMock, MagicMock

        mock_agent = MagicMock()
        mock_agent.run_loop = AsyncMock(return_value="done")

        mock_registry = MagicMock()
        mock_registry.resolve.return_value = mock_agent

        store = SQLiteEventStore(tmp_path / "test.db")

        ctx = DelegationContext(
            can_delegate=True,
            peer_registry=mock_registry,
            event_store=store,
        )
        tool = make_task_tool(ctx)
        result = json.loads(
            await tool.handler(prompt="task", target="agent")
        )
        assert result["ok"] is True

        # Verify child session was created
        session_id = result["session_id"]
        session = await store.get_session(session_id)
        assert session is not None

    @pytest.mark.asyncio
    async def test_delegation_failure_returns_error(self) -> None:
        """When delegate agent fails, tool returns error with message."""
        from unittest.mock import AsyncMock, MagicMock

        mock_agent = MagicMock()
        mock_agent.run_loop = AsyncMock(side_effect=RuntimeError("backend crashed"))

        mock_registry = MagicMock()
        mock_registry.resolve.return_value = mock_agent

        ctx = DelegationContext(
            can_delegate=True,
            peer_registry=mock_registry,
        )
        tool = make_task_tool(ctx)
        result = json.loads(
            await tool.handler(prompt="crash", target="agent")
        )
        assert result["ok"] is False
        assert result["error"] == "delegation_failed"
        assert "backend crashed" in result["message"]


# ---------------------------------------------------------------------------
# Tool allowlist enforcement in AgentLoop
# ---------------------------------------------------------------------------


class TestToolAllowlist:
    @pytest.mark.asyncio
    async def test_allowlist_blocks_unauthorized_tool(self) -> None:
        """Tools not in allowlist get UNAUTHORIZED error."""
        spec = ToolSpec(
            name="secret",
            description="Secret tool",
            parameters={},
            handler=lambda: "should not run",
        )
        # Only allow "safe" tool, not "secret"
        backend = MockBackend([
            # Turn 1: model tries to use "secret"
            [
                StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="secret"),
                StreamChunk(kind=ChunkKind.TOOL_USE_END),
                StreamChunk(kind=ChunkKind.DONE),
            ],
            _make_text_chunks("done"),
        ])
        loop = AgentLoop(
            backend,
            _make_registry(spec),
            max_turns=5,
            tool_allowlist=["safe"],
        )

        events: list[AgentEvent] = []
        async for event in loop.run("go"):
            events.append(event)

        # Should have a TOOL_RESULT with error
        results = [e for e in events if e.kind == AgentEventKind.TOOL_RESULT]
        assert len(results) == 1
        assert results[0].is_error is True
        assert "not in allowlist" in results[0].tool_result

    @pytest.mark.asyncio
    async def test_allowlist_permits_authorized_tool(self) -> None:
        """Tools in allowlist execute normally."""
        spec = ToolSpec(
            name="safe",
            description="Safe tool",
            parameters={},
            handler=lambda: "safe result",
        )
        backend = MockBackend([
            [
                StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="safe"),
                StreamChunk(kind=ChunkKind.TOOL_USE_END),
                StreamChunk(kind=ChunkKind.DONE),
            ],
            _make_text_chunks("done"),
        ])
        loop = AgentLoop(
            backend,
            _make_registry(spec),
            max_turns=5,
            tool_allowlist=["safe"],
        )

        events: list[AgentEvent] = []
        async for event in loop.run("go"):
            events.append(event)

        results = [e for e in events if e.kind == AgentEventKind.TOOL_RESULT]
        assert len(results) == 1
        assert results[0].is_error is False
        assert "safe result" in results[0].tool_result

    @pytest.mark.asyncio
    async def test_no_allowlist_allows_all(self) -> None:
        """When tool_allowlist is None, all tools execute."""
        spec = ToolSpec(
            name="any_tool",
            description="Any tool",
            parameters={},
            handler=lambda: "works",
        )
        backend = MockBackend([
            [
                StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="any_tool"),
                StreamChunk(kind=ChunkKind.TOOL_USE_END),
                StreamChunk(kind=ChunkKind.DONE),
            ],
            _make_text_chunks("done"),
        ])
        loop = AgentLoop(
            backend,
            _make_registry(spec),
            max_turns=5,
            tool_allowlist=None,  # No restriction
        )

        events: list[AgentEvent] = []
        async for event in loop.run("go"):
            events.append(event)

        results = [e for e in events if e.kind == AgentEventKind.TOOL_RESULT]
        assert len(results) == 1
        assert results[0].is_error is False
