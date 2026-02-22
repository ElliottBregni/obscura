"""Tests for sdk.backends.mcp_backend — MCPBackend and MCPBackendMixin.

Comprehensive test suite covering initialization, lifecycle, tools,
hooks, sessions, send/stream, and server management.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    HookContext,
    HookPoint,
    SessionRef,
    ToolSpec,
)
from obscura.providers.mcp_backend import MCPBackend, MCPBackendMixin
from obscura.integrations.mcp.types import MCPConnectionConfig, MCPError, MCPTransportType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def echo_handler(**kwargs: Any) -> dict[str, Any]:
    """Simple async handler that echoes its keyword arguments."""
    return {"echo": kwargs}


async def failing_handler(**kwargs: Any) -> None:
    """Handler that always raises."""
    raise RuntimeError("boom")


def make_tool(name: str = "test_tool", handler: Any = None) -> ToolSpec:
    """Create a ToolSpec with sensible defaults."""
    return ToolSpec(
        name=name,
        description=f"Description for {name}",
        parameters={"type": "object", "properties": {}},
        handler=handler or echo_handler,
    )


# ===========================================================================
# 1. TestMCPBackendInit
# ===========================================================================


class TestMCPBackendInit:
    """Verify MCPBackend constructor defaults and custom values."""

    def test_default_init(self) -> None:
        backend = MCPBackend()
        assert backend.name == "mcp"
        assert backend.mcp_servers == []
        assert isinstance(backend.tools, list)
        assert len(backend.tools) == 0

    def test_custom_name(self) -> None:
        backend = MCPBackend(name="custom")
        assert backend.name == "custom"

    def test_empty_tools_on_init(self) -> None:
        backend = MCPBackend(mcp_servers=[])
        assert backend.list_tools() == []

    def test_hooks_initialized(self) -> None:
        backend = MCPBackend()
        for hp in HookPoint:
            assert hp in backend.hooks, f"Missing hook point: {hp}"
            assert backend.hooks[hp] == []

    def test_not_initialized_on_init(self) -> None:
        backend = MCPBackend()
        assert backend.initialized is False

    def test_with_servers(self) -> None:
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        backend = MCPBackend(mcp_servers=[config])
        assert len(backend.mcp_servers) == 1
        assert backend.mcp_servers[0] is config

    def test_none_servers_becomes_empty_list(self) -> None:
        backend = MCPBackend(mcp_servers=None)
        assert backend.mcp_servers == []

    def test_tool_registry_created(self) -> None:
        backend = MCPBackend()
        assert isinstance(backend.tool_registry, ToolRegistry)


# ===========================================================================
# 2. TestMCPBackendSendStream
# ===========================================================================


class TestMCPBackendSendStream:
    """send() and stream() should raise NotImplementedError."""

    @pytest.mark.asyncio
    async def test_send_raises_not_implemented(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        with pytest.raises(NotImplementedError, match="does not support direct LLM"):
            await backend.send("hello")

    @pytest.mark.asyncio
    async def test_stream_raises_not_implemented(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        with pytest.raises(NotImplementedError, match="does not support direct LLM"):
            await backend.stream("hello")


# ===========================================================================
# 3. TestMCPBackendSessions
# ===========================================================================


class TestMCPBackendSessions:
    """Session operations are unsupported except list_sessions (returns [])."""

    @pytest.mark.asyncio
    async def test_create_session_raises(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        with pytest.raises(NotImplementedError, match="does not support sessions"):
            await backend.create_session()

    @pytest.mark.asyncio
    async def test_resume_session_raises(self) -> None:
        from obscura.core.types import Backend

        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        ref = SessionRef(session_id="s1", backend=Backend.COPILOT)
        with pytest.raises(NotImplementedError, match="does not support sessions"):
            await backend.resume_session(ref)

    @pytest.mark.asyncio
    async def test_list_sessions_returns_empty(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        result = await backend.list_sessions()
        assert result == []

    @pytest.mark.asyncio
    async def test_delete_session_raises(self) -> None:
        from obscura.core.types import Backend

        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        ref = SessionRef(session_id="s1", backend=Backend.COPILOT)
        with pytest.raises(NotImplementedError, match="does not support sessions"):
            await backend.delete_session(ref)


# ===========================================================================
# 4. TestMCPBackendTools
# ===========================================================================


class TestMCPBackendTools:
    """Tool registration, listing, and registry access."""

    def test_register_tool(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        spec = make_tool("my_tool")
        backend.register_tool(spec)

        assert len(backend.tools) == 1
        assert backend.tools[0].name == "my_tool"
        assert "my_tool" in backend.tool_registry

    def test_list_tools(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        spec = make_tool("tool_a")
        backend.register_tool(spec)

        tools = backend.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "tool_a"

    def test_list_tools_is_copy(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        spec = make_tool("tool_a")
        backend.register_tool(spec)

        returned = backend.list_tools()
        returned.clear()
        # Internal list must remain intact.
        assert len(backend.tools) == 1

    def test_get_tool_registry(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        registry = backend.get_tool_registry()
        assert isinstance(registry, ToolRegistry)

    def test_register_multiple_tools(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        for name in ("a", "b", "c"):
            backend.register_tool(make_tool(name))

        assert len(backend.tools) == 3
        assert len(backend.tool_registry) == 3

    def test_tool_registry_lookup(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        spec = make_tool("lookup_me")
        backend.register_tool(spec)

        found = backend.get_tool_registry().get("lookup_me")
        assert found is not None
        assert found.name == "lookup_me"


# ===========================================================================
# 5. TestMCPBackendCallTool
# ===========================================================================


class TestMCPBackendCallTool:
    """Tool execution via call_tool()."""

    @pytest.mark.asyncio
    async def test_call_tool_executes_handler(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        spec = make_tool("test_tool", handler=echo_handler)
        backend.register_tool(spec)

        result = await backend.call_tool("test_tool", {"msg": "hi"})
        assert result == {"echo": {"msg": "hi"}}

    @pytest.mark.asyncio
    async def test_call_tool_not_found(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        with pytest.raises(MCPError) as exc_info:
            await backend.call_tool("nonexistent", {})
        assert exc_info.value.code == -32003
        assert "not found" in exc_info.value.message.lower()

    @pytest.mark.asyncio
    async def test_call_tool_fires_hooks(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")

        pre_hook: Any = MagicMock()
        post_hook: Any = MagicMock()
        backend.register_hook(HookPoint.PRE_TOOL_USE, pre_hook)
        backend.register_hook(HookPoint.POST_TOOL_USE, post_hook)

        spec = make_tool("hooked_tool", handler=echo_handler)
        backend.register_tool(spec)

        await backend.call_tool("hooked_tool", {"x": 1})

        pre_hook.assert_called_once()
        pre_ctx = pre_hook.call_args[0][0]
        assert isinstance(pre_ctx, HookContext)
        assert pre_ctx.hook == HookPoint.PRE_TOOL_USE
        assert pre_ctx.tool_name == "hooked_tool"

        post_hook.assert_called_once()
        post_ctx = post_hook.call_args[0][0]
        assert post_ctx.hook == HookPoint.POST_TOOL_USE
        assert post_ctx.tool_output == {"echo": {"x": 1}}

    @pytest.mark.asyncio
    async def test_call_tool_passes_arguments(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")

        async def adder(**kwargs: Any) -> Any:
            return kwargs["a"] + kwargs["b"]

        spec = make_tool("adder", handler=adder)
        backend.register_tool(spec)

        result = await backend.call_tool("adder", {"a": 3, "b": 4})
        assert result == 7

    @pytest.mark.asyncio
    async def test_call_tool_handler_error_propagates(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        spec = make_tool("bad_tool", handler=failing_handler)
        backend.register_tool(spec)

        with pytest.raises(RuntimeError, match="boom"):
            await backend.call_tool("bad_tool", {})


# ===========================================================================
# 6. TestMCPBackendHooks
# ===========================================================================


class TestMCPBackendHooks:
    """Hook registration and execution, including error resilience."""

    def test_register_hook(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        cb: Any = MagicMock()
        backend.register_hook(HookPoint.PRE_TOOL_USE, cb)
        assert cb in backend.hooks[HookPoint.PRE_TOOL_USE]

    @pytest.mark.asyncio
    async def test_run_hooks_sync(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        sync_cb: Any = MagicMock()
        backend.register_hook(HookPoint.PRE_TOOL_USE, sync_cb)

        spec = make_tool("t")
        backend.register_tool(spec)
        await backend.call_tool("t", {})

        sync_cb.assert_called_once()
        ctx = sync_cb.call_args[0][0]
        assert ctx.hook == HookPoint.PRE_TOOL_USE

    @pytest.mark.asyncio
    async def test_run_hooks_async(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        called_with: list[Any] = []

        async def async_cb(context: Any) -> None:
            called_with.append(context)

        backend.register_hook(HookPoint.PRE_TOOL_USE, async_cb)

        spec = make_tool("t")
        backend.register_tool(spec)
        await backend.call_tool("t", {})

        assert len(called_with) == 1
        assert called_with[0].hook == HookPoint.PRE_TOOL_USE

    @pytest.mark.asyncio
    async def test_hook_error_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")

        def exploding_hook(ctx: Any) -> None:
            raise ValueError("hook exploded")

        backend.register_hook(HookPoint.PRE_TOOL_USE, exploding_hook)

        spec = make_tool("t")
        backend.register_tool(spec)

        # The hook error must not propagate to the caller.
        with caplog.at_level(logging.WARNING):
            result = await backend.call_tool("t", {})

        assert result == {"echo": {}}
        assert any(
            "hook exploded" in r.message.lower() or "Hook failed" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_async_hook_error_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")

        async def async_exploding_hook(ctx: Any) -> None:
            raise RuntimeError("async hook exploded")

        backend.register_hook(HookPoint.POST_TOOL_USE, async_exploding_hook)

        spec = make_tool("t")
        backend.register_tool(spec)

        with caplog.at_level(logging.WARNING):
            result = await backend.call_tool("t", {})

        assert result == {"echo": {}}
        assert any(
            "async hook exploded" in r.message.lower() or "Hook failed" in r.message
            for r in caplog.records
        )

    def test_register_hook_multiple(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        cb1: Any = MagicMock()
        cb2: Any = MagicMock()
        backend.register_hook(HookPoint.STOP, cb1)
        backend.register_hook(HookPoint.STOP, cb2)
        assert len(backend.hooks[HookPoint.STOP]) == 2


# ===========================================================================
# 7. TestMCPBackendLifecycle
# ===========================================================================


class TestMCPBackendLifecycle:
    """Start/stop lifecycle, idempotency, and cleanup."""

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        mock_mgr: Any = MagicMock()
        mock_mgr.aggregate_tools = AsyncMock(return_value=[])
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        await backend.start()
        assert backend.initialized is True

        # Call start again -- should return early, not call aggregate_tools twice.
        await backend.start()
        mock_mgr.aggregate_tools.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_clears_tools(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        backend.register_tool(make_tool("t1"))
        backend.register_tool(make_tool("t2"))
        assert len(backend.tools) == 2

        mock_mgr: Any = MagicMock()
        mock_mgr.close_all = AsyncMock()
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        await backend.stop()
        assert backend.tools == []

    @pytest.mark.asyncio
    async def test_stop_sets_not_initialized(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        backend._initialized = True  # pyright: ignore[reportPrivateUsage]

        mock_mgr: Any = MagicMock()
        mock_mgr.close_all = AsyncMock()
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        await backend.stop()
        assert backend.initialized is False

    @pytest.mark.asyncio
    async def test_start_connects_servers(self) -> None:
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        backend = MCPBackend(mcp_servers=[config], name="test-mcp")
        mock_mgr: Any = MagicMock()
        mock_mgr.add_session = AsyncMock()
        mock_mgr.aggregate_tools = AsyncMock(return_value=[])
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        await backend.start()
        mock_mgr.add_session.assert_called_once_with(
            "mcp_server_0", config
        )

    @pytest.mark.asyncio
    async def test_start_with_server_failure_still_initializes(self) -> None:
        config = MCPConnectionConfig(
            transport=MCPTransportType.STDIO, command="nonexistent"
        )
        backend = MCPBackend(mcp_servers=[config], name="test-mcp")
        mock_mgr: Any = MagicMock()
        mock_mgr.add_session = AsyncMock(
            side_effect=Exception("connection failed")
        )
        mock_mgr.aggregate_tools = AsyncMock(return_value=[])
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        # Should not raise; logs the error and continues.
        await backend.start()
        assert backend.initialized is True

    @pytest.mark.asyncio
    async def test_stop_calls_close_all(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        mock_mgr: Any = MagicMock()
        mock_mgr.close_all = AsyncMock()
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        await backend.stop()
        mock_mgr.close_all.assert_awaited_once()


# ===========================================================================
# 8. TestMCPBackendServers
# ===========================================================================


class TestMCPBackendServers:
    """Server listing and health-check via mocked session manager."""

    def test_list_servers(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        mock_mgr: Any = MagicMock()
        mock_mgr.list_sessions.return_value = [
            "server_a",
            "server_b",
        ]
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        result = backend.list_servers()
        assert result == ["server_a", "server_b"]

    @pytest.mark.asyncio
    async def test_health_check_healthy(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        mock_mgr: Any = MagicMock()
        mock_mgr.list_sessions.return_value = ["srv1"]

        mock_client: Any = AsyncMock()
        mock_client.ping = AsyncMock(return_value={})
        mock_mgr.get_session.return_value = mock_client
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        health = await backend.health_check()
        assert health["srv1"]["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_health_check_disconnected(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        mock_mgr: Any = MagicMock()
        mock_mgr.list_sessions.return_value = ["srv_gone"]
        mock_mgr.get_session.return_value = None
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        health = await backend.health_check()
        assert health["srv_gone"]["status"] == "disconnected"

    @pytest.mark.asyncio
    async def test_health_check_unhealthy(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        mock_mgr: Any = MagicMock()
        mock_mgr.list_sessions.return_value = ["srv_bad"]

        mock_client: Any = AsyncMock()
        mock_client.ping = AsyncMock(side_effect=Exception("timeout"))
        mock_mgr.get_session.return_value = mock_client
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        health = await backend.health_check()
        assert health["srv_bad"]["status"] == "unhealthy"
        assert "timeout" in health["srv_bad"]["error"]

    @pytest.mark.asyncio
    async def test_health_check_empty_servers(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        mock_mgr: Any = MagicMock()
        mock_mgr.list_sessions.return_value = []
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        health = await backend.health_check()
        assert health == {}

    @pytest.mark.asyncio
    async def test_add_server(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        mock_mgr: Any = MagicMock()
        mock_mgr.list_sessions.return_value = []
        mock_mgr.add_session = AsyncMock()
        mock_mgr.aggregate_tools = AsyncMock(return_value=[])
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        config = MCPConnectionConfig(
            transport=MCPTransportType.SSE, url="http://localhost:3000"
        )
        session_name = await backend.add_server(config)
        assert session_name == "mcp_server_0"
        mock_mgr.add_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_remove_server(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        mock_mgr: Any = MagicMock()
        mock_mgr.remove_session = AsyncMock()
        mock_mgr.aggregate_tools = AsyncMock(return_value=[])
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        await backend.remove_server("mcp_server_0")
        mock_mgr.remove_session.assert_awaited_once_with("mcp_server_0")


# ===========================================================================
# 9. TestMCPBackendMCPToolConversion
# ===========================================================================


class TestMCPBackendMCPToolConversion:
    """_mcp_tool_to_obscura conversion logic."""

    def test_with_session_prefix(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        mcp_tool: Any = MagicMock()
        mcp_tool.name = "server1.read_file"
        mcp_tool.description = "Read a file"
        mcp_tool.inputSchema = {"type": "object"}

        result = backend._mcp_tool_to_obscura(mcp_tool)  # pyright: ignore[reportPrivateUsage]
        assert result.name == "server1.read_file"
        assert result.description == "Read a file"
        assert result.parameters == {"type": "object"}
        assert callable(result.handler)

    def test_without_session_prefix(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        mcp_tool: Any = MagicMock()
        mcp_tool.name = "read_file"
        mcp_tool.description = "Read a file"
        mcp_tool.inputSchema = {}

        result = backend._mcp_tool_to_obscura(mcp_tool)  # pyright: ignore[reportPrivateUsage]
        assert result.name == "read_file"

    @pytest.mark.asyncio
    async def test_converted_handler_calls_execute_mcp_tool(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        mcp_tool: Any = MagicMock()
        mcp_tool.name = "srv.do_thing"
        mcp_tool.description = "Do a thing"
        mcp_tool.inputSchema = {}

        tool_spec = backend._mcp_tool_to_obscura(mcp_tool)  # pyright: ignore[reportPrivateUsage]

        # Mock _execute_mcp_tool to verify it gets called
        backend._execute_mcp_tool = AsyncMock(return_value="done")  # pyright: ignore[reportPrivateUsage]

        result = await tool_spec.handler(arg1="val1")
        backend._execute_mcp_tool.assert_awaited_once_with(  # pyright: ignore[reportPrivateUsage]
            "srv", "do_thing", {"arg1": "val1"}
        )
        assert result == "done"


# ===========================================================================
# 10. TestMCPBackendExecuteMCPTool
# ===========================================================================


class TestMCPBackendExecuteMCPTool:
    """_execute_mcp_tool session lookup and error handling."""

    @pytest.mark.asyncio
    async def test_session_not_found_raises(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        mock_mgr: Any = MagicMock()
        mock_mgr.get_session.return_value = None
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(MCPError) as exc_info:
            await backend._execute_mcp_tool("missing", "tool", {})  # pyright: ignore[reportPrivateUsage]
        assert exc_info.value.code == -32000
        assert "not found" in exc_info.value.message.lower()

    @pytest.mark.asyncio
    async def test_successful_execution(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        mock_client: Any = AsyncMock()
        mock_result: Any = MagicMock()
        mock_client.call_tool.return_value = mock_result

        mock_mgr: Any = MagicMock()
        mock_mgr.get_session.return_value = mock_client
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        with patch(
            "obscura.providers.mcp_backend.mcp_result_to_obscura", return_value="converted"
        ):
            result = await backend._execute_mcp_tool("srv", "tool_name", {"a": 1})  # pyright: ignore[reportPrivateUsage]

        mock_client.call_tool.assert_awaited_once_with("tool_name", {"a": 1})
        assert result == "converted"


# ===========================================================================
# 11. TestMCPBackendMixin
# ===========================================================================


class TestMCPBackendMixin:
    """MCPBackendMixin wiring for composite backends."""

    def test_mixin_creates_internal_backend(self) -> None:
        mixin = MCPBackendMixin.__new__(MCPBackendMixin)
        MCPBackendMixin.__init__(mixin, mcp_servers=[])
        assert isinstance(mixin._mcp_backend, MCPBackend)  # pyright: ignore[reportPrivateUsage]
        assert mixin._mcp_tools_added is False  # pyright: ignore[reportPrivateUsage]

    def test_mixin_passes_servers(self) -> None:
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        mixin = MCPBackendMixin.__new__(MCPBackendMixin)
        MCPBackendMixin.__init__(mixin, mcp_servers=[config])
        assert len(mixin._mcp_backend.mcp_servers) == 1  # pyright: ignore[reportPrivateUsage]

    def test_mixin_none_servers(self) -> None:
        mixin = MCPBackendMixin.__new__(MCPBackendMixin)
        MCPBackendMixin.__init__(mixin, mcp_servers=None)
        assert mixin._mcp_backend.mcp_servers == []  # pyright: ignore[reportPrivateUsage]


# ===========================================================================
# 12. TestMCPBackendRefreshTools
# ===========================================================================


class TestMCPBackendRefreshTools:
    """_refresh_tools aggregates tools from the session manager."""

    @pytest.mark.asyncio
    async def test_refresh_tools_populates_tools(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")

        mcp_tool: Any = MagicMock()
        mcp_tool.name = "srv.my_tool"
        mcp_tool.description = "A tool"
        mcp_tool.inputSchema = {}

        mock_mgr: Any = MagicMock()
        mock_mgr.aggregate_tools = AsyncMock(return_value=[mcp_tool])
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        await backend._refresh_tools()  # pyright: ignore[reportPrivateUsage]

        assert len(backend.tools) == 1
        assert backend.tools[0].name == "srv.my_tool"
        assert "srv.my_tool" in backend.tool_registry

    @pytest.mark.asyncio
    async def test_refresh_tools_clears_previous(self) -> None:
        backend = MCPBackend(mcp_servers=[], name="test-mcp")
        backend.register_tool(make_tool("old_tool"))
        assert len(backend.tools) == 1

        mock_mgr: Any = MagicMock()
        mock_mgr.aggregate_tools = AsyncMock(return_value=[])
        backend._session_manager = mock_mgr  # pyright: ignore[reportPrivateUsage]

        await backend._refresh_tools()  # pyright: ignore[reportPrivateUsage]
        assert len(backend.tools) == 0
        assert "old_tool" not in backend.tool_registry
