"""Tests for the grounded delegate_to_agent tool and agent cards section."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.agent.peers import (
    AgentRef,
    PeerRegistry,
    RemoteAgentRef,
    UnixSocketAgentRef,
)
from obscura.core.tools import ToolRegistry
from obscura.tools.system.delegation import (
    build_agent_cards_section,
    build_delegate_tool_spec,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mock_runtime(agents: list[AgentRef] | None = None) -> MagicMock:
    """Create a mock AgentRuntime with a mock PeerRegistry."""
    runtime = MagicMock()
    registry = MagicMock(spec=PeerRegistry)
    registry.discover.return_value = agents or []
    runtime.peer_registry = registry
    runtime.invoke_peer = AsyncMock(return_value="delegation result")
    return runtime


def _make_local_ref(
    name: str,
    model: str = "copilot",
    status: str = "RUNNING",
) -> AgentRef:
    return AgentRef(
        runtime_id="rt-test",
        agent_id=f"agent-{name}",
        name=name,
        model=model,
        status=status,
        capabilities=("local_invoke", "local_stream"),
    )


# ---------------------------------------------------------------------------
# build_delegate_tool_spec tests
# ---------------------------------------------------------------------------


class TestBuildDelegateToolSpec:
    def test_populates_enum_from_local_peers(self) -> None:
        agents = [
            _make_local_ref("code-reviewer"),
            _make_local_ref("explorer"),
            _make_local_ref("researcher"),
        ]
        runtime = _make_mock_runtime(agents)
        spec = build_delegate_tool_spec(runtime, runtime.peer_registry)

        assert spec.name == "delegate_to_agent"
        assert spec.required_tier == "privileged"
        enum = spec.parameters["properties"]["agent"]["enum"]
        assert sorted(enum) == ["code-reviewer", "explorer", "researcher"]

    def test_includes_remote_refs_in_enum(self) -> None:
        local = [_make_local_ref("local-agent")]
        remote = [RemoteAgentRef(url="http://remote:8080", name="remote-agent")]
        runtime = _make_mock_runtime(local)
        spec = build_delegate_tool_spec(
            runtime,
            runtime.peer_registry,
            remote_refs=remote,
        )

        enum = spec.parameters["properties"]["agent"]["enum"]
        assert "local-agent" in enum
        assert "remote-agent" in enum

    def test_includes_available_unix_socket_refs(self) -> None:
        local = [_make_local_ref("local-agent")]
        sockets = [
            UnixSocketAgentRef(
                socket_path="/tmp/test.sock",
                name="socket-agent",
                status="available",
            ),
            UnixSocketAgentRef(
                socket_path="/tmp/gone.sock",
                name="gone-agent",
                status="unavailable",
            ),
        ]
        runtime = _make_mock_runtime(local)
        spec = build_delegate_tool_spec(
            runtime,
            runtime.peer_registry,
            unix_socket_refs=sockets,
        )

        enum = spec.parameters["properties"]["agent"]["enum"]
        assert "socket-agent" in enum
        assert "gone-agent" not in enum

    def test_empty_peers_still_constructs(self) -> None:
        runtime = _make_mock_runtime([])
        spec = build_delegate_tool_spec(runtime, runtime.peer_registry)

        assert spec.name == "delegate_to_agent"
        assert "enum" not in spec.parameters["properties"]["agent"]

    def test_required_fields(self) -> None:
        runtime = _make_mock_runtime([_make_local_ref("a")])
        spec = build_delegate_tool_spec(runtime, runtime.peer_registry)

        assert spec.parameters["required"] == ["agent", "prompt"]
        assert "mode" in spec.parameters["properties"]


class TestDelegateHandler:
    @pytest.mark.asyncio
    async def test_local_routing(self) -> None:
        agents = [_make_local_ref("helper")]
        runtime = _make_mock_runtime(agents)
        spec = build_delegate_tool_spec(runtime, runtime.peer_registry)

        result = json.loads(
            await spec.handler(agent="helper", prompt="do something"),
        )
        assert result["ok"] is True
        assert result["transport"] == "local"
        assert result["result"] == "delegation result"
        runtime.invoke_peer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_agent_returns_error(self) -> None:
        runtime = _make_mock_runtime([_make_local_ref("known")])
        spec = build_delegate_tool_spec(runtime, runtime.peer_registry)

        result = json.loads(
            await spec.handler(agent="ghost", prompt="hello"),
        )
        assert result["ok"] is False
        assert result["error"] == "agent_not_found"
        assert "ghost" in result["message"]

    @pytest.mark.asyncio
    async def test_local_failure_returns_error(self) -> None:
        agents = [_make_local_ref("crasher")]
        runtime = _make_mock_runtime(agents)
        runtime.invoke_peer = AsyncMock(
            side_effect=RuntimeError("backend exploded"),
        )
        spec = build_delegate_tool_spec(runtime, runtime.peer_registry)

        result = json.loads(
            await spec.handler(agent="crasher", prompt="crash"),
        )
        assert result["ok"] is False
        assert result["error"] == "RuntimeError"
        assert "backend exploded" in result["message"]


# ---------------------------------------------------------------------------
# build_agent_cards_section tests
# ---------------------------------------------------------------------------


class TestAgentCardsSection:
    def test_lists_local_agents(self) -> None:
        agents = [
            _make_local_ref("code-reviewer", model="claude"),
            _make_local_ref("explorer", model="copilot"),
        ]
        runtime = _make_mock_runtime(agents)

        section = build_agent_cards_section(runtime.peer_registry)

        assert "## Available Agents for Delegation" in section
        assert "**code-reviewer**" in section
        assert "**explorer**" in section
        assert "claude" in section
        assert "delegate_to_agent" in section

    def test_includes_remote_agents(self) -> None:
        runtime = _make_mock_runtime([])
        remote = [
            RemoteAgentRef(
                url="http://remote:8080",
                name="analyst",
                description="data analysis",
            ),
        ]

        section = build_agent_cards_section(
            runtime.peer_registry,
            remote_refs=remote,
        )
        assert "**analyst**" in section
        assert "data analysis" in section

    def test_includes_unix_socket_agents(self) -> None:
        runtime = _make_mock_runtime([])
        sockets = [
            UnixSocketAgentRef(
                socket_path="/tmp/test.sock",
                name="socket-bot",
                status="available",
                description="local socket agent",
            ),
        ]

        section = build_agent_cards_section(
            runtime.peer_registry,
            unix_socket_refs=sockets,
        )
        assert "**socket-bot**" in section

    def test_empty_agents_message(self) -> None:
        runtime = _make_mock_runtime([])
        section = build_agent_cards_section(runtime.peer_registry)
        assert "No agents are currently available" in section


# ---------------------------------------------------------------------------
# Alias mapping tests
# ---------------------------------------------------------------------------


class TestAliasMapping:
    def test_delegate_alias_resolves(self) -> None:
        registry = ToolRegistry()
        # The alias should point to delegate_to_agent
        target = registry._alias_targets.get("delegate")
        assert target == "delegate_to_agent"

    def test_all_delegation_aliases(self) -> None:
        registry = ToolRegistry()
        aliases = [
            "delegate",
            "ask_agent",
            "spawn_agent",
            "invoke_agent",
            "delegate_task",
            "call_agent",
        ]
        for alias in aliases:
            target = registry._alias_targets.get(alias)
            assert target == "delegate_to_agent", (
                f"Alias '{alias}' should map to 'delegate_to_agent', got '{target}'"
            )
