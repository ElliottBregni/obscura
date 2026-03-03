"""Tests for sdk.routes.agents -- Agent CRUD, bulk ops, templates, tags, streaming."""

import asyncio
import pytest
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from starlette.testclient import TestClient
from fastapi import FastAPI

from obscura.core.config import ObscuraConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_templates() -> Generator[None, None, None]:  # pyright: ignore[reportUnusedFunction]
    """Reset the in-memory template store between tests."""
    from obscura.routes.agents import clear_agent_templates

    clear_agent_templates()
    yield
    clear_agent_templates()


@pytest.fixture
def app() -> FastAPI:
    config = ObscuraConfig(auth_enabled=False, otel_enabled=False)
    from obscura.server import create_app

    return create_app(config)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _make_mock_agent(
    agent_id: str = "agent-1",
    name: str = "test-agent",
    model: str = "copilot",
    status_name: str = "WAITING",
    tags: list[str] | None = None,
) -> MagicMock:
    """Build a MagicMock that looks like an Agent."""
    mock: MagicMock = MagicMock()
    mock.id = agent_id
    mock.config = MagicMock()
    mock.config.name = name
    mock.config.model = model
    mock.config.tags = tags or []
    mock.status = MagicMock()
    mock.status.name = status_name
    mock.created_at = MagicMock()
    mock.created_at.isoformat.return_value = "2026-01-01T00:00:00+00:00"
    mock.start = AsyncMock()
    mock.stop = AsyncMock()
    mock.run = AsyncMock(return_value="result text")
    mock.list_registered_tools = MagicMock(return_value=[])
    mock.discover_peers = AsyncMock(return_value=MagicMock(local=[], remote=[]))
    return mock


def _make_mock_runtime(agents: list[MagicMock] | None = None) -> AsyncMock:
    """Build a MagicMock that looks like an AgentRuntime."""
    runtime: AsyncMock = AsyncMock()
    agents = agents or []
    # spawn returns an agent
    runtime.spawn = MagicMock(
        return_value=(agents[0] if agents else _make_mock_agent())
    )
    # get_agent looks up by id
    _agents_by_id: dict[str, MagicMock] = {a.id: a for a in agents}

    def _get_agent(aid: str) -> MagicMock | None:
        return _agents_by_id.get(aid)

    runtime.get_agent = MagicMock(side_effect=_get_agent)
    # list_agents returns the list
    runtime.list_agents = MagicMock(return_value=agents)
    # get_agent_status
    runtime.get_agent_status = MagicMock(return_value=None)
    return runtime


# ---------------------------------------------------------------------------
# Agent spawn (POST /agents)
# ---------------------------------------------------------------------------


class TestAgentSpawn:
    @patch("obscura.routes.agents.get_runtime")
    def test_spawn_agent_defaults(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        agent = _make_mock_agent()
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post("/api/v1/agents", json={"name": "test-agent"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "agent-1"
        assert data["name"] == "test-agent"
        assert data["mcp_enabled"] is False
        assert runtime.spawn.call_args.kwargs["enable_system_tools"] is True
        assert runtime.spawn.call_args.kwargs["a2a_remote_tools"] == {}

    @patch("obscura.routes.agents.get_runtime")
    def test_spawn_agent_with_model(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        agent = _make_mock_agent(model="claude")
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents",
            json={
                "name": "claude-agent",
                "model": "claude",
            },
        )
        assert resp.status_code == 200

    @patch("obscura.routes.agents.get_runtime")
    def test_spawn_agent_invalid_model(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Line 35: invalid model raises 400."""
        runtime = _make_mock_runtime()
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents",
            json={
                "name": "bad",
                "model": "gpt-999",
            },
        )
        assert resp.status_code == 400
        assert "Invalid model" in resp.json()["detail"]

    @patch("obscura.routes.agents.get_runtime")
    def test_spawn_agent_with_mcp(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        agent = _make_mock_agent()
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents",
            json={
                "name": "mcp-agent",
                "model": "copilot",
                "mcp": {
                    "enabled": True,
                    "servers": [{"transport": "stdio"}],
                    "config_path": "config/mcp-config.json",
                    "server_names": ["playwright"],
                    "primary_server_name": "github",
                    "auto_discover": True,
                    "resolve_env": True,
                },
            },
        )
        assert resp.status_code == 200
        assert resp.json()["mcp_enabled"] is True
        mcp_cfg = runtime.spawn.call_args.kwargs["mcp"]
        assert mcp_cfg.enabled is True
        assert mcp_cfg.servers == [{"transport": "stdio"}]
        assert mcp_cfg.config_path == "config/mcp-config.json"
        assert mcp_cfg.server_names == ["playwright"]
        assert mcp_cfg.primary_server_name == "github"
        assert mcp_cfg.auto_discover is True
        assert mcp_cfg.resolve_env is True
        assert runtime.spawn.call_args.kwargs["enable_system_tools"] is True

    @patch("obscura.routes.agents.get_runtime")
    def test_spawn_agent_with_system_tools_disabled(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        agent = _make_mock_agent()
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents",
            json={"name": "no-system-tools", "enable_system_tools": False},
        )
        assert resp.status_code == 200
        assert runtime.spawn.call_args.kwargs["enable_system_tools"] is False

    @patch("obscura.routes.agents.get_runtime")
    def test_spawn_agent_with_a2a_remote_tools(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        agent = _make_mock_agent()
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        payload = {
            "name": "remote-tools",
            "a2a_remote_tools": {
                "enabled": True,
                "urls": ["https://agent-one.local", "https://agent-two.local"],
                "auth_token": "demo",
            },
        }
        resp = client.post("/api/v1/agents", json=payload)
        assert resp.status_code == 200
        assert (
            runtime.spawn.call_args.kwargs["a2a_remote_tools"]
            == payload["a2a_remote_tools"]
        )

    @patch("obscura.routes.agents.get_runtime")
    def test_spawn_agent_with_builder_payload(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        agent = _make_mock_agent(model="claude")
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        payload = {
            "builder": {
                "name": "builder-agent",
                "model": "claude",
                "system_prompt": "You are a planner.",
                "skills": [{"name": "planning", "content": "Always plan first."}],
                "memory_namespace": "builder:claude:planner",
                "max_iterations": 22,
                "timeout_seconds": 91.5,
                "enable_system_tools": False,
                "tags": ["builder", "aper"],
                "parent_agent_id": "agent-root",
                "mcp_auto_discover": True,
                "mcp_config_path": "config/mcp-config.json",
                "mcp_server_names": ["github", "filesystem"],
                "mcp_primary_server_name": "github",
                "mcp_resolve_env": True,
                "a2a_remote_tools": {
                    "enabled": True,
                    "urls": ["https://a2a.example.local"],
                    "auth_token": "token-1",
                },
            }
        }
        resp = client.post("/api/v1/agents", json=payload)
        assert resp.status_code == 200
        kwargs = runtime.spawn.call_args.kwargs
        assert kwargs["name"] == "builder-agent"
        assert kwargs["model"] == "claude"
        assert kwargs["memory_namespace"] == "builder:claude:planner"
        assert kwargs["max_iterations"] == 22
        assert kwargs["timeout_seconds"] == 91.5
        assert kwargs["enable_system_tools"] is False
        assert kwargs["tags"] == ["builder", "aper"]
        assert kwargs["parent_agent_id"] == "agent-root"
        assert "## Loaded Skills" in kwargs["system_prompt"]
        assert "Always plan first." in kwargs["system_prompt"]
        assert kwargs["a2a_remote_tools"]["enabled"] is True
        assert kwargs["mcp"].enabled is True
        assert kwargs["mcp"].auto_discover is True
        assert kwargs["mcp"].server_names == ["github", "filesystem"]

    @patch("obscura.routes.agents.get_runtime")
    def test_spawn_agent_top_level_overrides_builder(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        agent = _make_mock_agent(model="openai")
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents",
            json={
                "name": "top-agent",
                "model": "openai",
                "system_prompt": "Top level prompt.",
                "enable_system_tools": True,
                "builder": {
                    "name": "builder-agent",
                    "model": "claude",
                    "system_prompt": "Builder prompt",
                    "enable_system_tools": False,
                    "skills": [{"name": "s1", "content": "S1"}],
                },
            },
        )
        assert resp.status_code == 200
        kwargs = runtime.spawn.call_args.kwargs
        assert kwargs["name"] == "top-agent"
        assert kwargs["model"] == "openai"
        assert kwargs["enable_system_tools"] is True
        assert kwargs["system_prompt"].startswith("Top level prompt.")


# ---------------------------------------------------------------------------
# Agent get (GET /agents/{agent_id})
# ---------------------------------------------------------------------------


class TestAgentGet:
    @patch("obscura.routes.agents.get_runtime")
    def test_get_agent_found(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 73-86: happy path."""
        state = MagicMock()
        state.agent_id = "agent-1"
        state.name = "my-agent"
        state.status.name = "RUNNING"
        state.created_at.isoformat.return_value = "2026-01-01T00:00:00+00:00"
        state.updated_at.isoformat.return_value = "2026-01-01T00:01:00+00:00"
        state.iteration_count = 3
        state.error_message = None

        runtime = AsyncMock()
        runtime.get_agent_status = MagicMock(return_value=state)
        mock_get_runtime.return_value = runtime

        resp = client.get("/api/v1/agents/agent-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "agent-1"
        assert data["iteration_count"] == 3
        assert data["error_message"] is None

    @patch("obscura.routes.agents.get_runtime")
    def test_get_agent_not_found(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 76-77: 404 path."""
        runtime = AsyncMock()
        runtime.get_agent_status = MagicMock(return_value=None)
        mock_get_runtime.return_value = runtime

        resp = client.get("/api/v1/agents/nonexistent")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]


class TestAgentRunLoop:
    @patch("obscura.routes.agents.get_runtime")
    def test_run_loop_with_tool_approval_sets_on_confirm(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        agent = _make_mock_agent()
        captured_on_confirm: object | None = None

        async def _fake_run_loop(prompt: str, **kwargs: Any) -> str:
            nonlocal captured_on_confirm
            _ = prompt
            captured_on_confirm = kwargs.get("on_confirm")
            return "loop-result"

        agent.run_loop = _fake_run_loop
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents/agent-1/run",
            json={
                "prompt": "hi",
                "mode": "loop",
                "require_tool_approval": True,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["result"] == "loop-result"
        assert callable(captured_on_confirm)


class TestAgentListTools:
    @patch("obscura.routes.agents.get_runtime")
    def test_list_tools_success(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        agent = _make_mock_agent()
        fake_tool = MagicMock()
        fake_tool.name = "run_python3"
        fake_tool.description = "Execute python code"
        fake_tool.required_tier = "privileged"
        fake_tool.parameters = {"type": "object"}
        agent.list_registered_tools = MagicMock(return_value=[fake_tool])

        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.get("/api/v1/agents/agent-1/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "agent-1"
        assert len(data["tools"]) == 1
        assert data["tools"][0]["name"] == "run_python3"
        assert data["tools"][0]["required_tier"] == "privileged"

    @patch("obscura.routes.agents.get_runtime")
    def test_list_tools_not_found(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        runtime = AsyncMock()
        runtime.get_agent = MagicMock(return_value=None)
        mock_get_runtime.return_value = runtime

        resp = client.get("/api/v1/agents/nonexistent/tools")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]


class TestAgentListPeers:
    @patch("obscura.routes.agents.get_runtime")
    def test_list_peers_success(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        agent = _make_mock_agent()
        local_ref = MagicMock()
        local_ref.model_dump.return_value = {
            "kind": "local",
            "agent_id": "agent-2",
        }
        remote_ref = MagicMock()
        remote_ref.model_dump.return_value = {
            "kind": "a2a_remote",
            "url": "https://a2a.local",
            "status": "configured",
        }
        catalog = MagicMock(local=[local_ref], remote=[remote_ref])
        agent.discover_peers = AsyncMock(return_value=catalog)

        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.get(
            "/api/v1/agents/agent-1/peers?include_self=false&discover_remote=false"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "agent-1"
        assert len(data["local"]) == 1
        assert len(data["remote"]) == 1
        assert data["local"][0]["kind"] == "local"
        assert data["remote"][0]["kind"] == "a2a_remote"

    @patch("obscura.routes.agents.get_runtime")
    def test_list_peers_not_found(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        runtime = AsyncMock()
        runtime.get_agent = MagicMock(return_value=None)
        mock_get_runtime.return_value = runtime

        resp = client.get("/api/v1/agents/nonexistent/peers")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Agent run (POST /agents/{agent_id}/run)
# ---------------------------------------------------------------------------


class TestAgentRun:
    @patch("obscura.routes.agents.get_runtime")
    def test_run_agent_success(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 97-112: happy path."""
        agent = _make_mock_agent()
        agent.run = AsyncMock(return_value="some result")
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents/agent-1/run",
            json={
                "prompt": "do something",
                "context": {"key": "value"},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "agent-1"
        assert data["result"] == "some result"

    @patch("obscura.routes.agents.get_runtime")
    def test_run_agent_not_found(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 100-101: 404."""
        runtime = _make_mock_runtime()
        runtime.get_agent = MagicMock(return_value=None)
        mock_get_runtime.return_value = runtime

        resp = client.post("/api/v1/agents/missing/run", json={"prompt": "hi"})
        assert resp.status_code == 404

    @patch("obscura.routes.agents.get_runtime")
    def test_run_agent_exception(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 113-114: 500 on agent.run error."""
        agent = _make_mock_agent()
        agent.run = AsyncMock(side_effect=RuntimeError("boom"))
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post("/api/v1/agents/agent-1/run", json={"prompt": "fail"})
        assert resp.status_code == 500
        assert "boom" in resp.json()["detail"]

    @patch("obscura.routes.agents.get_runtime")
    def test_run_agent_timeout(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        agent = _make_mock_agent()

        async def _slow_run(*_args: object, **_kwargs: object) -> str:
            await asyncio.sleep(0.05)
            return "late"

        agent.run = AsyncMock(side_effect=_slow_run)
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents/agent-1/run",
            json={"prompt": "slow", "timeout_seconds": 0.01},
        )
        assert resp.status_code == 504
        assert "timed out" in resp.json()["detail"]

    @patch("obscura.routes.agents.get_runtime")
    def test_run_agent_timeout_invalid(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        agent = _make_mock_agent()
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents/agent-1/run",
            json={"prompt": "x", "timeout_seconds": 0},
        )
        assert resp.status_code == 400
        assert "timeout_seconds" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Agent stop (DELETE /agents/{agent_id})
# ---------------------------------------------------------------------------


class TestAgentStop:
    @patch("obscura.routes.agents.get_runtime")
    def test_stop_agent_success(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 123-135."""
        agent = _make_mock_agent()
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.delete("/api/v1/agents/agent-1")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
        agent.stop.assert_awaited_once()

    @patch("obscura.routes.agents.get_runtime")
    def test_stop_agent_not_found(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 126-127: 404."""
        runtime = _make_mock_runtime()
        runtime.get_agent = MagicMock(return_value=None)
        mock_get_runtime.return_value = runtime

        resp = client.delete("/api/v1/agents/missing")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Agent list (GET /agents)
# ---------------------------------------------------------------------------


class TestAgentList:
    @patch("obscura.routes.agents.get_runtime")
    def test_list_agents_empty(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        runtime = _make_mock_runtime()
        runtime.list_agents = MagicMock(return_value=[])
        mock_get_runtime.return_value = runtime

        resp = client.get("/api/v1/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agents"] == []
        assert data["count"] == 0

    @patch("obscura.routes.agents.get_runtime")
    def test_list_agents_with_results(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        a1 = _make_mock_agent("a1", "first", "copilot", tags=["prod"])
        a2 = _make_mock_agent("a2", "second", "claude", tags=["dev"])
        runtime = _make_mock_runtime([a1, a2])
        mock_get_runtime.return_value = runtime

        resp = client.get("/api/v1/agents")
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

    @patch("obscura.routes.agents.get_runtime")
    def test_list_agents_filter_by_invalid_status(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 152-155: invalid status => 400."""
        runtime = _make_mock_runtime()
        runtime.list_agents = MagicMock(return_value=[])
        mock_get_runtime.return_value = runtime

        resp = client.get("/api/v1/agents?status=BOGUS")
        assert resp.status_code == 400
        assert "Invalid status" in resp.json()["detail"]

    @patch("obscura.routes.agents.get_runtime")
    def test_list_agents_filter_by_valid_status(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 152-153: valid status passes through."""
        a1 = _make_mock_agent("a1", "first")
        runtime = _make_mock_runtime([a1])
        mock_get_runtime.return_value = runtime

        resp = client.get("/api/v1/agents?status=RUNNING")
        assert resp.status_code == 200

    @patch("obscura.routes.agents.get_runtime")
    def test_list_agents_filter_by_tags(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 160-164: tag filter."""
        a1 = _make_mock_agent("a1", "first", tags=["prod", "ml"])
        a2 = _make_mock_agent("a2", "second", tags=["dev"])
        runtime = _make_mock_runtime([a1, a2])
        mock_get_runtime.return_value = runtime

        resp = client.get("/api/v1/agents?tags=prod")
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

    @patch("obscura.routes.agents.get_runtime")
    def test_list_agents_filter_by_name(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Line 167: name filter (case-insensitive substring)."""
        a1 = _make_mock_agent("a1", "ReviewerBot")
        a2 = _make_mock_agent("a2", "CodeWriter")
        runtime = _make_mock_runtime([a1, a2])
        mock_get_runtime.return_value = runtime

        resp = client.get("/api/v1/agents?name=reviewer")
        assert resp.status_code == 200
        assert resp.json()["count"] == 1
        assert resp.json()["agents"][0]["name"] == "ReviewerBot"


# ---------------------------------------------------------------------------
# Bulk spawn (POST /agents/bulk)
# ---------------------------------------------------------------------------


class TestBulkSpawn:
    @patch("obscura.routes.agents.get_runtime")
    def test_bulk_spawn_success(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 197-229: happy path."""
        agent = _make_mock_agent()
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents/bulk",
            json={
                "agents": [
                    {"name": "a1", "model": "claude"},
                    {"name": "a2", "model": "copilot"},
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_requested"] == 2
        assert data["total_created"] == 2

    @patch("obscura.routes.agents.get_runtime")
    def test_bulk_spawn_empty(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 200-201: empty list => 400."""
        runtime = _make_mock_runtime()
        mock_get_runtime.return_value = runtime

        resp = client.post("/api/v1/agents/bulk", json={"agents": []})
        assert resp.status_code == 400
        assert "No agents provided" in resp.json()["detail"]

    @patch("obscura.routes.agents.get_runtime")
    def test_bulk_spawn_too_many(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 202-203: >100 agents => 400."""
        runtime = _make_mock_runtime()
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents/bulk",
            json={"agents": [{"name": f"a{i}"} for i in range(101)]},
        )
        assert resp.status_code == 400
        assert "Cannot spawn more than 100" in resp.json()["detail"]

    @patch("obscura.routes.agents.get_runtime")
    def test_bulk_spawn_partial_failure(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 226-227: one agent fails, others succeed."""
        call_count = 0

        def spawn_side_effect(**kwargs: object):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("spawn failed")
            return _make_mock_agent(agent_id=f"agent-{call_count}")

        runtime = AsyncMock()
        runtime.spawn = MagicMock(side_effect=spawn_side_effect)
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents/bulk",
            json={
                "agents": [
                    {"name": "a1"},
                    {"name": "a2"},
                    {"name": "a3"},
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_created"] == 2
        assert len(data["errors"]) == 1
        assert data["errors"][0]["index"] == 1


# ---------------------------------------------------------------------------
# Bulk stop (POST /agents/bulk/stop)
# ---------------------------------------------------------------------------


class TestBulkStop:
    @patch("obscura.routes.agents.get_runtime")
    def test_bulk_stop_success(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 243-264."""
        a1 = _make_mock_agent("a1")
        a2 = _make_mock_agent("a2")
        runtime = _make_mock_runtime([a1, a2])
        mock_get_runtime.return_value = runtime

        resp = client.post("/api/v1/agents/bulk/stop", json={"agent_ids": ["a1", "a2"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_stopped"] == 2
        assert data["stopped"] == ["a1", "a2"]

    @patch("obscura.routes.agents.get_runtime")
    def test_bulk_stop_empty(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 246-247: empty list => 400."""
        runtime = _make_mock_runtime()
        mock_get_runtime.return_value = runtime

        resp = client.post("/api/v1/agents/bulk/stop", json={"agent_ids": []})
        assert resp.status_code == 400

    @patch("obscura.routes.agents.get_runtime")
    def test_bulk_stop_agent_not_found(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 255-256: agent not found goes to errors."""
        runtime = _make_mock_runtime()
        runtime.get_agent = MagicMock(return_value=None)
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents/bulk/stop", json={"agent_ids": ["nonexistent"]}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_stopped"] == 0
        assert len(data["errors"]) == 1

    @patch("obscura.routes.agents.get_runtime")
    def test_bulk_stop_with_exception(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 261-262: stop raises exception."""
        agent = _make_mock_agent("a1")
        agent.stop = AsyncMock(side_effect=RuntimeError("cleanup failed"))
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post("/api/v1/agents/bulk/stop", json={"agent_ids": ["a1"]})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["errors"]) == 1
        assert "cleanup failed" in data["errors"][0]["error"]


# ---------------------------------------------------------------------------
# Bulk tag (POST /agents/bulk/tag)
# ---------------------------------------------------------------------------


class TestBulkTag:
    @patch("obscura.routes.agents.get_runtime")
    def test_bulk_tag_success(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 278-303."""
        a1 = _make_mock_agent("a1", tags=["existing"])
        a2 = _make_mock_agent("a2", tags=[])
        runtime = _make_mock_runtime([a1, a2])
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents/bulk/tag",
            json={
                "agent_ids": ["a1", "a2"],
                "tags": ["new-tag"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "a1" in data["tagged"]
        assert "a2" in data["tagged"]

    @patch("obscura.routes.agents.get_runtime")
    def test_bulk_tag_no_agent_ids(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 282-283: empty agent_ids => 400."""
        runtime = _make_mock_runtime()
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents/bulk/tag",
            json={
                "agent_ids": [],
                "tags": ["t1"],
            },
        )
        assert resp.status_code == 400

    @patch("obscura.routes.agents.get_runtime")
    def test_bulk_tag_no_tags(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 284-285: empty tags => 400."""
        runtime = _make_mock_runtime()
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents/bulk/tag",
            json={
                "agent_ids": ["a1"],
                "tags": [],
            },
        )
        assert resp.status_code == 400

    @patch("obscura.routes.agents.get_runtime")
    def test_bulk_tag_agent_not_found(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 293-294: agent not found goes to errors."""
        runtime = _make_mock_runtime()
        runtime.get_agent = MagicMock(return_value=None)
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents/bulk/tag",
            json={
                "agent_ids": ["missing"],
                "tags": ["x"],
            },
        )
        assert resp.status_code == 200
        assert len(resp.json()["errors"]) == 1


# ---------------------------------------------------------------------------
# Templates (CRUD)
# ---------------------------------------------------------------------------


class TestAgentTemplates:
    def test_create_template(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/agent-templates",
            json={
                "name": "reviewer",
                "model": "copilot",
                "system_prompt": "You review code.",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "reviewer"
        assert "template_id" in data

    def test_list_templates(self, client: TestClient) -> None:
        client.post("/api/v1/agent-templates", json={"name": "t1", "model": "copilot"})
        resp = client.get("/api/v1/agent-templates")
        assert resp.status_code == 200
        data = resp.json()
        assert "templates" in data
        assert isinstance(data["templates"], list)
        assert data["count"] >= 1

    def test_get_template(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/v1/agent-templates", json={"name": "t2", "model": "claude"}
        )
        tid = create_resp.json()["template_id"]
        resp = client.get(f"/api/v1/agent-templates/{tid}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "t2"

    def test_get_template_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/agent-templates/nonexistent")
        assert resp.status_code == 404

    def test_delete_template(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/v1/agent-templates", json={"name": "t3", "model": "copilot"}
        )
        tid = create_resp.json()["template_id"]
        resp = client.delete(f"/api/v1/agent-templates/{tid}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_delete_template_not_found(self, client: TestClient) -> None:
        """Line 372: template not found => 404."""
        resp = client.delete("/api/v1/agent-templates/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Spawn from template (POST /agents/from-template)
# ---------------------------------------------------------------------------


class TestSpawnFromTemplate:
    @patch("obscura.routes.agents.get_runtime")
    def test_spawn_from_template_success(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 387-411."""
        # Create a template first
        tmpl = client.post(
            "/api/v1/agent-templates",
            json={
                "name": "reviewer",
                "model": "claude",
                "system_prompt": "review code",
            },
        )
        tid = tmpl.json()["template_id"]

        agent = _make_mock_agent()
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents/from-template",
            json={
                "template_id": tid,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["template_id"] == tid

    @patch("obscura.routes.agents.get_runtime")
    def test_spawn_from_template_no_id(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 390-391: missing template_id => 400."""
        runtime = _make_mock_runtime()
        mock_get_runtime.return_value = runtime

        resp = client.post("/api/v1/agents/from-template", json={})
        assert resp.status_code == 422  # Pydantic validation: template_id required

    @patch("obscura.routes.agents.get_runtime")
    def test_spawn_from_template_not_found(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 394-395: template not found => 404."""
        runtime = _make_mock_runtime()
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents/from-template",
            json={
                "template_id": "nonexistent",
            },
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tags (per-agent)
# ---------------------------------------------------------------------------


class TestAgentTags:
    @patch("obscura.routes.agents.get_runtime")
    def test_add_tags(self, mock_get_runtime: MagicMock, client: TestClient) -> None:
        """Lines 430-447."""
        agent = _make_mock_agent(tags=["existing"])
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents/agent-1/tags",
            json={
                "tags": ["new", "another"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "new" in data["tags"]
        assert "another" in data["tags"]
        assert "existing" in data["tags"]

    @patch("obscura.routes.agents.get_runtime")
    def test_add_tags_not_found(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 433-434: 404."""
        runtime = _make_mock_runtime()
        runtime.get_agent = MagicMock(return_value=None)
        mock_get_runtime.return_value = runtime

        resp = client.post("/api/v1/agents/missing/tags", json={"tags": ["t"]})
        assert resp.status_code == 404

    @patch("obscura.routes.agents.get_runtime")
    def test_add_tags_empty(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 437-438: empty tags => 400."""
        agent = _make_mock_agent()
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post("/api/v1/agents/agent-1/tags", json={"tags": []})
        assert resp.status_code == 400

    @patch("obscura.routes.agents.get_runtime")
    def test_add_tags_no_existing(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 440-441: agent.config has no tags attr."""
        agent = _make_mock_agent()
        del agent.config.tags  # simulate missing attr
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post("/api/v1/agents/agent-1/tags", json={"tags": ["new"]})
        assert resp.status_code == 200

    @patch("obscura.routes.agents.get_runtime")
    def test_remove_tags(self, mock_get_runtime: MagicMock, client: TestClient) -> None:
        """Lines 461-478."""
        agent = _make_mock_agent(tags=["keep", "remove-me"])
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post(
            "/api/v1/agents/agent-1/tags/remove",
            json={
                "tags": ["remove-me"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "remove-me" in data["removed"]
        assert "remove-me" not in data["tags"]

    @patch("obscura.routes.agents.get_runtime")
    def test_remove_tags_not_found(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 464-465: 404."""
        runtime = _make_mock_runtime()
        runtime.get_agent = MagicMock(return_value=None)
        mock_get_runtime.return_value = runtime

        resp = client.post("/api/v1/agents/missing/tags/remove", json={"tags": ["x"]})
        assert resp.status_code == 404

    @patch("obscura.routes.agents.get_runtime")
    def test_remove_tags_empty_list(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 468-469: empty tags => 400."""
        agent = _make_mock_agent()
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post("/api/v1/agents/agent-1/tags/remove", json={"tags": []})
        assert resp.status_code == 400

    @patch("obscura.routes.agents.get_runtime")
    def test_remove_tags_no_existing(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 471-472: agent has no tags attr => returns empty."""
        agent = _make_mock_agent()
        del agent.config.tags
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post("/api/v1/agents/agent-1/tags/remove", json={"tags": ["x"]})
        assert resp.status_code == 200
        assert resp.json()["tags"] == []
        assert resp.json()["removed"] == []

    @patch("obscura.routes.agents.get_runtime")
    def test_get_tags(self, mock_get_runtime: MagicMock, client: TestClient) -> None:
        """Lines 491-499."""
        agent = _make_mock_agent(tags=["alpha", "beta"])
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.get("/api/v1/agents/agent-1/tags")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["tags"]) == {"alpha", "beta"}

    @patch("obscura.routes.agents.get_runtime")
    def test_get_tags_not_found(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 494-495: 404."""
        runtime = _make_mock_runtime()
        runtime.get_agent = MagicMock(return_value=None)
        mock_get_runtime.return_value = runtime

        resp = client.get("/api/v1/agents/missing/tags")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Stream (POST /agents/{agent_id}/stream)
# ---------------------------------------------------------------------------


class TestAgentStream:
    @patch("obscura.routes.agents.get_runtime")
    def test_stream_agent_not_found(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 518-519: 404."""
        runtime = _make_mock_runtime()
        runtime.get_agent = MagicMock(return_value=None)
        mock_get_runtime.return_value = runtime

        resp = client.post("/api/v1/agents/missing/stream", json={"prompt": "hi"})
        assert resp.status_code == 404

    @patch("obscura.routes.agents.get_runtime")
    def test_stream_agent_returns_sse(
        self, mock_get_runtime: MagicMock, client: TestClient
    ) -> None:
        """Lines 515-532: SSE event source response."""
        agent = _make_mock_agent()

        async def fake_stream(prompt: str, **ctx: object):
            yield "chunk-1"
            yield "chunk-2"

        agent.stream = fake_stream
        runtime = _make_mock_runtime([agent])
        mock_get_runtime.return_value = runtime

        resp = client.post("/api/v1/agents/agent-1/stream", json={"prompt": "test"})
        # EventSourceResponse returns 200 with text/event-stream content type
        assert resp.status_code == 200
