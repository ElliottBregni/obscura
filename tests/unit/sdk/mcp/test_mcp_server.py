"""Tests for sdk.mcp.server — ObscuraMCPServer."""

# pyright: reportPrivateUsage=false, reportMissingParameterType=false, reportUnknownParameterType=false
import json
import pytest
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

from sdk.mcp.server import ObscuraMCPServer, create_mcp_router
from sdk.mcp.types import (
    MCPError,
    MCPErrorCode,
    ObscuraMCPConfig,
    ObscuraMCPToolContext,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user():
    """Create a mock AuthenticatedUser."""
    user = MagicMock()
    user.user_id = "u-123"
    user.email = "test@example.com"
    user.roles = ("admin",)
    user.org_id = None
    user.token_type = "user"
    user.raw_token = "tok"
    return user


def _make_agent(
    agent_id="agent-1",
    name="helper",
    model="claude",
    namespace="default",
    status_name="RUNNING",
):
    agent = MagicMock()
    agent.id = agent_id
    agent.config.name = name
    agent.config.model = model
    agent.config.memory_namespace = namespace
    agent.config.tags = ["test"]
    agent.status.name = status_name
    agent.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    agent.updated_at = datetime(2025, 1, 2, tzinfo=UTC)
    agent.iteration_count = 5
    agent.start = AsyncMock()
    agent.stop = AsyncMock()
    agent.stop_graceful = AsyncMock()
    return agent


def _make_memory_store():
    store = MagicMock()
    store.get = MagicMock(return_value=None)
    store.set = MagicMock()
    store.delete = MagicMock(return_value=True)
    store.list_keys = MagicMock(return_value=[])
    store.search = MagicMock(return_value=[])
    return store


# ---------------------------------------------------------------------------
# Init tests
# ---------------------------------------------------------------------------


class TestObscuraMCPServerInit:
    def test_defaults(self):
        server = ObscuraMCPServer()
        assert server._initialized is False
        assert server._runtime is None
        assert server.user is None

    def test_with_config(self):
        config = ObscuraMCPConfig()
        server = ObscuraMCPServer(config=config)
        assert server.config is config

    def test_with_user(self):
        user = _make_user()
        server = ObscuraMCPServer(user=user)
        assert server.user is user


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


class TestObscuraMCPServerLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_no_user(self):
        server = ObscuraMCPServer()
        await server.initialize()
        assert server._initialized is True
        assert server._runtime is None  # No user, no runtime

    @pytest.mark.asyncio
    async def test_initialize_idempotent(self):
        server = ObscuraMCPServer()
        await server.initialize()
        await server.initialize()  # Should not raise
        assert server._initialized is True

    @pytest.mark.asyncio
    async def test_shutdown(self):
        server = ObscuraMCPServer()
        await server.initialize()
        await server.shutdown()
        assert server._initialized is False

    @pytest.mark.asyncio
    async def test_initialize_with_user_creates_runtime(self):
        user = _make_user()
        server = ObscuraMCPServer()
        with patch("sdk.mcp.server.AgentRuntime") as MockRT:
            mock_rt_inst = MagicMock()
            mock_rt_inst.start = AsyncMock()
            MockRT.return_value = mock_rt_inst
            await server.initialize(user=user)
            assert server.user is user
            assert server._runtime is mock_rt_inst
            mock_rt_inst.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_with_runtime(self):
        server = ObscuraMCPServer()
        mock_rt = MagicMock()
        mock_rt.shutdown = AsyncMock()
        mock_rt.stop = AsyncMock()
        server._runtime = mock_rt
        server._initialized = True
        await server.shutdown()
        assert mock_rt.shutdown.await_count + mock_rt.stop.await_count == 1
        assert server._runtime is None
        assert server._initialized is False


# ---------------------------------------------------------------------------
# Protocol tests
# ---------------------------------------------------------------------------


class TestObscuraMCPServerProtocol:
    @pytest.mark.asyncio
    async def test_handle_initialize(self):
        server = ObscuraMCPServer()
        result = await server.handle_initialize(
            protocolVersion="2024-11-05",
            capabilities={},
            clientInfo={"name": "test", "version": "1.0"},
        )
        assert result["protocolVersion"] == "2024-11-05"
        assert "capabilities" in result
        assert "serverInfo" in result
        assert result["serverInfo"]["name"] == "obscura-mcp"

    @pytest.mark.asyncio
    async def test_handle_tools_list(self):
        server = ObscuraMCPServer()
        tools = await server.handle_tools_list()
        assert isinstance(tools, list)
        tool_names = [t["name"] for t in tools]
        assert "list_agents" in tool_names
        assert "spawn_agent" in tool_names
        assert "get_memory" in tool_names
        assert "set_memory" in tool_names
        assert "delete_memory" in tool_names
        assert "search_memory" in tool_names
        assert "stop_agent" in tool_names
        assert "get_agent_status" in tool_names

    @pytest.mark.asyncio
    async def test_handle_tools_call_list_agents_no_runtime(self):
        server = ObscuraMCPServer()
        context = ObscuraMCPToolContext(user_id="test")
        result = await server.handle_tools_call("list_agents", {}, context)
        assert result is not None

    @pytest.mark.asyncio
    async def test_handle_tools_call_default_context(self):
        """When no context passed, an anonymous context is created."""
        server = ObscuraMCPServer()
        result = await server.handle_tools_call("list_agents", {})
        assert result is not None

    @pytest.mark.asyncio
    async def test_handle_prompts_list(self):
        server = ObscuraMCPServer()
        prompts = await server.handle_prompts_list()
        assert isinstance(prompts, list)
        assert len(prompts) >= 2
        names = [p["name"] for p in prompts]
        assert "agent_task" in names
        assert "memory_query" in names

    @pytest.mark.asyncio
    async def test_handle_prompts_get_agent_task(self):
        server = ObscuraMCPServer()
        result = await server.handle_prompts_get(
            "agent_task",
            {"task": "Run some tests"},
        )
        assert "messages" in result
        assert len(result["messages"]) == 1
        assert "Run some tests" in result["messages"][0]["content"]["text"]

    @pytest.mark.asyncio
    async def test_handle_prompts_get_agent_task_default(self):
        """agent_task with no task argument uses default."""
        server = ObscuraMCPServer()
        result = await server.handle_prompts_get("agent_task", {})
        assert "Execute the task" in result["messages"][0]["content"]["text"]

    @pytest.mark.asyncio
    async def test_handle_prompts_get_memory_query(self):
        server = ObscuraMCPServer()
        result = await server.handle_prompts_get(
            "memory_query",
            {"query": "user preferences"},
        )
        assert "messages" in result
        assert "user preferences" in result["messages"][0]["content"]["text"]

    @pytest.mark.asyncio
    async def test_handle_prompts_get_memory_query_default(self):
        """memory_query with no query argument uses empty string."""
        server = ObscuraMCPServer()
        result = await server.handle_prompts_get("memory_query", {})
        assert "messages" in result

    @pytest.mark.asyncio
    async def test_handle_prompts_get_not_found(self):
        server = ObscuraMCPServer()
        with pytest.raises(MCPError) as exc_info:
            await server.handle_prompts_get("nonexistent")
        assert exc_info.value.code == MCPErrorCode.PROMPT_NOT_FOUND.value

    @pytest.mark.asyncio
    async def test_handle_prompts_get_none_arguments(self):
        """Passing None arguments should use defaults."""
        server = ObscuraMCPServer()
        result = await server.handle_prompts_get("agent_task", None)
        assert "messages" in result

    @pytest.mark.asyncio
    async def test_handle_resources_list_no_user(self):
        server = ObscuraMCPServer()
        resources = await server.handle_resources_list()
        assert resources == []

    @pytest.mark.asyncio
    async def test_handle_resources_read_unknown_uri(self):
        server = ObscuraMCPServer()
        with pytest.raises(MCPError) as exc_info:
            await server.handle_resources_read("unknown://foo")
        assert exc_info.value.code == MCPErrorCode.RESOURCE_NOT_FOUND.value


# ---------------------------------------------------------------------------
# Tool handlers with mocked runtime/user
# ---------------------------------------------------------------------------


class TestObscuraMCPServerToolHandlers:
    @pytest.mark.asyncio
    async def test_handle_list_agents_no_runtime(self):
        server = ObscuraMCPServer()
        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_list_agents(ctx, {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_handle_list_agents_with_runtime(self):
        server = ObscuraMCPServer()
        agent = _make_agent()
        mock_runtime = MagicMock()
        mock_runtime.list_agents.return_value = [agent]
        server._runtime = mock_runtime

        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_list_agents(ctx, {})
        assert result["count"] == 1
        assert result["agents"][0]["id"] == "agent-1"
        assert result["agents"][0]["name"] == "helper"

    @pytest.mark.asyncio
    async def test_handle_list_agents_namespace_filter_match(self):
        server = ObscuraMCPServer()
        agent = _make_agent(namespace="project")
        mock_runtime = MagicMock()
        mock_runtime.list_agents.return_value = [agent]
        server._runtime = mock_runtime

        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_list_agents(ctx, {"namespace": "project"})
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_handle_list_agents_namespace_filter_no_match(self):
        server = ObscuraMCPServer()
        agent = _make_agent(namespace="default")
        mock_runtime = MagicMock()
        mock_runtime.list_agents.return_value = [agent]
        server._runtime = mock_runtime

        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_list_agents(ctx, {"namespace": "other"})
        assert result["count"] == 0
        assert result["agents"] == []

    @pytest.mark.asyncio
    async def test_handle_spawn_agent_no_runtime(self):
        server = ObscuraMCPServer()
        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_spawn_agent(
            ctx, {"name": "a1", "model": "claude"}
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_handle_spawn_agent_no_user(self):
        server = ObscuraMCPServer()
        server._runtime = MagicMock()
        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_spawn_agent(
            ctx, {"name": "a1", "model": "claude"}
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_handle_spawn_agent_success(self):
        server = ObscuraMCPServer()
        server.user = _make_user()
        agent = _make_agent()
        mock_runtime = MagicMock()
        mock_runtime.spawn.return_value = agent
        server._runtime = mock_runtime

        ctx = ObscuraMCPToolContext(user_id="test")
        with patch("sdk.mcp.server.AgentConfig") as MockConfig:
            MockConfig.return_value = MagicMock()
            result = await server._handle_spawn_agent(
                ctx,
                {
                    "name": "my-agent",
                    "model": "claude",
                    "system_prompt": "do stuff",
                    "memory_namespace": "proj",
                    "tags": ["a", "b"],
                },
            )
        assert result["agent_id"] == "agent-1"
        assert result["name"] == "helper"
        agent.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_stop_agent_no_runtime(self):
        server = ObscuraMCPServer()
        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_stop_agent(ctx, {"agent_id": "a1"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_handle_stop_agent_not_found(self):
        server = ObscuraMCPServer()
        mock_runtime = MagicMock()
        mock_runtime.get_agent.return_value = None
        server._runtime = mock_runtime

        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_stop_agent(ctx, {"agent_id": "missing"})
        assert "error" in result
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_handle_stop_agent_graceful(self):
        server = ObscuraMCPServer()
        agent = _make_agent()
        mock_runtime = MagicMock()
        mock_runtime.get_agent.return_value = agent
        server._runtime = mock_runtime

        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_stop_agent(ctx, {"agent_id": "agent-1"})
        assert result["stopped"] is True
        agent.stop_graceful.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_stop_agent_force(self):
        server = ObscuraMCPServer()
        agent = _make_agent()
        mock_runtime = MagicMock()
        mock_runtime.get_agent.return_value = agent
        server._runtime = mock_runtime

        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_stop_agent(
            ctx, {"agent_id": "agent-1", "force": True}
        )
        assert result["stopped"] is True
        agent.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_get_memory_no_user(self):
        server = ObscuraMCPServer()
        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_get_memory(ctx, {"key": "k1"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_handle_get_memory_found(self):
        server = ObscuraMCPServer()
        server.user = _make_user()
        store = _make_memory_store()
        store.get.return_value = {"data": "hello"}

        ctx = ObscuraMCPToolContext(user_id="test")
        with patch("sdk.mcp.server.MemoryStore") as MockMS:
            MockMS.for_user.return_value = store
            result = await server._handle_get_memory(
                ctx, {"key": "k1", "namespace": "proj"}
            )
        assert result["found"] is True
        assert result["key"] == "k1"
        assert result["value"] == {"data": "hello"}

    @pytest.mark.asyncio
    async def test_handle_get_memory_not_found(self):
        server = ObscuraMCPServer()
        server.user = _make_user()
        store = _make_memory_store()
        store.get.return_value = None

        ctx = ObscuraMCPToolContext(user_id="test")
        with patch("sdk.mcp.server.MemoryStore") as MockMS:
            MockMS.for_user.return_value = store
            result = await server._handle_get_memory(ctx, {"key": "missing"})
        assert result["found"] is False

    @pytest.mark.asyncio
    async def test_handle_set_memory_no_user(self):
        server = ObscuraMCPServer()
        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_set_memory(ctx, {"key": "k1", "value": "v1"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_handle_set_memory_json_value(self):
        server = ObscuraMCPServer()
        server.user = _make_user()
        store = _make_memory_store()

        ctx = ObscuraMCPToolContext(user_id="test")
        with patch("sdk.mcp.server.MemoryStore") as MockMS:
            MockMS.for_user.return_value = store
            result = await server._handle_set_memory(
                ctx,
                {
                    "key": "k1",
                    "value": '{"x": 1}',
                    "namespace": "ns",
                },
            )
        assert result["success"] is True
        store.set.assert_called_once_with("k1", {"x": 1}, namespace="ns")

    @pytest.mark.asyncio
    async def test_handle_set_memory_plain_string_value(self):
        server = ObscuraMCPServer()
        server.user = _make_user()
        store = _make_memory_store()

        ctx = ObscuraMCPToolContext(user_id="test")
        with patch("sdk.mcp.server.MemoryStore") as MockMS:
            MockMS.for_user.return_value = store
            result = await server._handle_set_memory(
                ctx,
                {
                    "key": "k1",
                    "value": "not-json",
                },
            )
        assert result["success"] is True
        # Non-JSON string stays as string
        store.set.assert_called_once_with("k1", "not-json", namespace="default")

    @pytest.mark.asyncio
    async def test_handle_delete_memory_no_user(self):
        server = ObscuraMCPServer()
        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_delete_memory(ctx, {"key": "k1"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_handle_delete_memory_success(self):
        server = ObscuraMCPServer()
        server.user = _make_user()
        store = _make_memory_store()
        store.delete.return_value = True

        ctx = ObscuraMCPToolContext(user_id="test")
        with patch("sdk.mcp.server.MemoryStore") as MockMS:
            MockMS.for_user.return_value = store
            result = await server._handle_delete_memory(
                ctx, {"key": "k1", "namespace": "ns"}
            )
        assert result["success"] is True
        assert result["key"] == "k1"

    @pytest.mark.asyncio
    async def test_handle_delete_memory_not_found(self):
        server = ObscuraMCPServer()
        server.user = _make_user()
        store = _make_memory_store()
        store.delete.return_value = False

        ctx = ObscuraMCPToolContext(user_id="test")
        with patch("sdk.mcp.server.MemoryStore") as MockMS:
            MockMS.for_user.return_value = store
            result = await server._handle_delete_memory(ctx, {"key": "missing"})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_handle_search_memory_no_user(self):
        server = ObscuraMCPServer()
        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_search_memory(ctx, {"query": "test"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_handle_search_memory_all_namespaces(self):
        server = ObscuraMCPServer()
        server.user = _make_user()
        store = _make_memory_store()
        # search returns list of (MemoryKey, value) tuples
        mk = MagicMock()
        mk.__str__.return_value = "default:config"
        store.search.return_value = [(mk, {"setting": True})]

        ctx = ObscuraMCPToolContext(user_id="test")
        with patch("sdk.mcp.server.MemoryStore") as MockMS:
            MockMS.for_user.return_value = store
            result = await server._handle_search_memory(ctx, {"query": "config"})
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_handle_search_memory_specific_namespace(self):
        server = ObscuraMCPServer()
        server.user = _make_user()
        store = _make_memory_store()
        mk = MagicMock()
        mk.key = "myconfig"
        mk.namespace = "proj"
        store.list_keys.return_value = [mk]
        store.get.return_value = {"val": 1}

        ctx = ObscuraMCPToolContext(user_id="test")
        with patch("sdk.mcp.server.MemoryStore") as MockMS:
            MockMS.for_user.return_value = store
            result = await server._handle_search_memory(
                ctx,
                {
                    "query": "myconfig",
                    "namespace": "proj",
                },
            )
        assert result["count"] == 1
        assert result["results"][0]["key"] == "myconfig"

    @pytest.mark.asyncio
    async def test_handle_search_memory_namespace_no_match(self):
        server = ObscuraMCPServer()
        server.user = _make_user()
        store = _make_memory_store()
        mk = MagicMock()
        mk.key = "unrelated"
        mk.namespace = "proj"
        store.list_keys.return_value = [mk]

        ctx = ObscuraMCPToolContext(user_id="test")
        with patch("sdk.mcp.server.MemoryStore") as MockMS:
            MockMS.for_user.return_value = store
            result = await server._handle_search_memory(
                ctx,
                {
                    "query": "missing",
                    "namespace": "proj",
                },
            )
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_handle_get_agent_status_no_runtime(self):
        server = ObscuraMCPServer()
        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_get_agent_status(ctx, {"agent_id": "a1"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_handle_get_agent_status_found(self):
        server = ObscuraMCPServer()
        agent = _make_agent()
        mock_runtime = MagicMock()
        mock_runtime.get_agent.return_value = agent
        server._runtime = mock_runtime

        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_get_agent_status(ctx, {"agent_id": "agent-1"})
        assert result["agent_id"] == "agent-1"
        assert result["name"] == "helper"
        assert result["tags"] == ["test"]

    @pytest.mark.asyncio
    async def test_handle_get_agent_status_not_found(self):
        server = ObscuraMCPServer()
        mock_runtime = MagicMock()
        mock_runtime.get_agent.return_value = None
        server._runtime = mock_runtime

        ctx = ObscuraMCPToolContext(user_id="test")
        result = await server._handle_get_agent_status(ctx, {"agent_id": "missing"})
        assert "error" in result


# ---------------------------------------------------------------------------
# Resources with mocked user/memory
# ---------------------------------------------------------------------------


class TestObscuraMCPServerResources:
    @pytest.mark.asyncio
    async def test_resources_list_with_user(self):
        server = ObscuraMCPServer()
        server.user = _make_user()
        store = _make_memory_store()
        mk1 = MagicMock()
        mk1.namespace = "project"
        mk2 = MagicMock()
        mk2.namespace = "default"
        mk3 = MagicMock()
        mk3.namespace = "project"  # duplicate namespace
        store.list_keys.return_value = [mk1, mk2, mk3]

        with patch("sdk.mcp.server.MemoryStore") as MockMS:
            MockMS.for_user.return_value = store
            resources = await server.handle_resources_list()

        assert len(resources) == 2  # deduplicated
        uris = [r["uri"] for r in resources]
        assert "memory://default" in uris
        assert "memory://project" in uris

    @pytest.mark.asyncio
    async def test_resources_read_memory_with_key(self):
        server = ObscuraMCPServer()
        server.user = _make_user()
        store = _make_memory_store()
        store.get.return_value = {"config": "value"}

        with patch("sdk.mcp.server.MemoryStore") as MockMS:
            MockMS.for_user.return_value = store
            result = await server.handle_resources_read("memory://proj/mykey")

        assert "contents" in result
        assert len(result["contents"]) == 1
        text = json.loads(result["contents"][0]["text"])
        assert text == {"config": "value"}

    @pytest.mark.asyncio
    async def test_resources_read_memory_namespace_only(self):
        server = ObscuraMCPServer()
        server.user = _make_user()
        store = _make_memory_store()
        mk = MagicMock()
        mk.key = "k1"
        store.list_keys.return_value = [mk]

        with patch("sdk.mcp.server.MemoryStore") as MockMS:
            MockMS.for_user.return_value = store
            result = await server.handle_resources_read("memory://proj")

        contents = result["contents"]
        assert len(contents) == 1
        text = json.loads(contents[0]["text"])
        assert text["namespace"] == "proj"
        assert "k1" in text["keys"]

    @pytest.mark.asyncio
    async def test_resources_read_memory_key_not_found(self):
        server = ObscuraMCPServer()
        server.user = _make_user()
        store = _make_memory_store()
        store.get.return_value = None

        with patch("sdk.mcp.server.MemoryStore") as MockMS:
            MockMS.for_user.return_value = store
            with pytest.raises(MCPError) as exc_info:
                await server.handle_resources_read("memory://proj/missing")
        assert exc_info.value.code == MCPErrorCode.RESOURCE_NOT_FOUND.value

    @pytest.mark.asyncio
    async def test_resources_read_memory_no_user(self):
        server = ObscuraMCPServer()
        with pytest.raises(MCPError) as exc_info:
            await server.handle_resources_read("memory://proj/key")
        assert exc_info.value.code == MCPErrorCode.RESOURCE_ACCESS_DENIED.value


# ---------------------------------------------------------------------------
# create_mcp_router
# ---------------------------------------------------------------------------


def _make_rpc_client():
    """Create a TestClient with the MCP router, fixing forward-ref annotations."""
    from fastapi import FastAPI, Request
    from starlette.testclient import TestClient

    server = ObscuraMCPServer()
    router = create_mcp_router(server)

    # Fix forward-ref annotations caused by `from __future__ import annotations`
    # in server.py. FastAPI cannot resolve string annotations for Request.
    for route in router.routes:
        if hasattr(route, "endpoint"):
            annots = getattr(route.endpoint, "__annotations__", {})
            if annots.get("request") == "Request":
                annots["request"] = Request

    app = FastAPI()
    app.include_router(router)
    return TestClient(app), server


class TestCreateMCPRouter:
    def test_router_creation(self):
        server = ObscuraMCPServer()
        router = create_mcp_router(server)
        assert router is not None
        route_paths = [r.path for r in router.routes]
        assert "/mcp/rpc" in route_paths
        assert "/mcp/sse" in route_paths

    @pytest.mark.asyncio
    async def test_rpc_initialize(self):
        client, __server = _make_rpc_client()

        resp = client.post(
            "/mcp/rpc",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 1
        assert "result" in data

    @pytest.mark.asyncio
    async def test_rpc_tools_list(self):
        client, __server = _make_rpc_client()
        resp = client.post(
            "/mcp/rpc",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        tool_names = [t["name"] for t in data["result"]]
        assert "list_agents" in tool_names

    @pytest.mark.asyncio
    async def test_rpc_tools_call(self):
        client, __server = _make_rpc_client()
        resp = client.post(
            "/mcp/rpc",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "list_agents",
                    "arguments": {},
                },
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert "content" in data["result"]

    @pytest.mark.asyncio
    async def test_rpc_resources_list(self):
        client, __server = _make_rpc_client()
        resp = client.post(
            "/mcp/rpc",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/list",
                "params": {},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data

    @pytest.mark.asyncio
    async def test_rpc_resources_read_error(self):
        client, __server = _make_rpc_client()
        resp = client.post(
            "/mcp/rpc",
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "resources/read",
                "params": {"uri": "unknown://x"},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data

    @pytest.mark.asyncio
    async def test_rpc_prompts_list(self):
        client, __server = _make_rpc_client()
        resp = client.post(
            "/mcp/rpc",
            json={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "prompts/list",
                "params": {},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data

    @pytest.mark.asyncio
    async def test_rpc_prompts_get(self):
        client, __server = _make_rpc_client()
        resp = client.post(
            "/mcp/rpc",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "prompts/get",
                "params": {"name": "agent_task", "arguments": {"task": "hello"}},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert "messages" in data["result"]

    @pytest.mark.asyncio
    async def test_rpc_method_not_found(self):
        client, __server = _make_rpc_client()
        resp = client.post(
            "/mcp/rpc",
            json={
                "jsonrpc": "2.0",
                "id": 8,
                "method": "nonexistent/method",
                "params": {},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == MCPErrorCode.METHOD_NOT_FOUND.value

    @pytest.mark.asyncio
    async def test_rpc_internal_error(self):
        client, __server = _make_rpc_client()
        with patch.object(
            __server, "handle_initialize", side_effect=RuntimeError("boom")
        ):
            resp = client.post(
                "/mcp/rpc",
                json={
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "initialize",
                    "params": {},
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == MCPErrorCode.INTERNAL_ERROR.value


