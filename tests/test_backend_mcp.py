"""Tests for sdk.backends.mcp_backend — MCPBackend."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sdk._types import HookPoint, ToolSpec
from sdk.backends.mcp_backend import MCPBackend
from sdk.mcp.types import MCPConnectionConfig, MCPTransportType, MCPError


class TestMCPBackendInit:
    def test_defaults(self):
        b = MCPBackend()
        assert b.name == "mcp"
        assert b.mcp_servers == []
        assert b._initialized is False
        assert len(b._tools) == 0

    def test_with_name(self):
        b = MCPBackend(name="custom-mcp")
        assert b.name == "custom-mcp"

    def test_with_servers(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        b = MCPBackend(mcp_servers=[config])
        assert len(b.mcp_servers) == 1


class TestMCPBackendLifecycle:
    @pytest.mark.asyncio
    async def test_start_no_servers(self):
        b = MCPBackend()
        with patch.object(b._session_manager, "aggregate_tools", new_callable=AsyncMock, return_value=[]):
            await b.start()
            assert b._initialized is True

    @pytest.mark.asyncio
    async def test_start_already_initialized(self):
        b = MCPBackend()
        b._initialized = True
        await b.start()  # Should return early

    @pytest.mark.asyncio
    async def test_stop(self):
        b = MCPBackend()
        b._initialized = True
        with patch.object(b._session_manager, "close_all", new_callable=AsyncMock):
            await b.stop()
            assert b._initialized is False
            assert len(b._tools) == 0

    @pytest.mark.asyncio
    async def test_start_with_server_failure(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="nonexistent")
        b = MCPBackend(mcp_servers=[config])
        with patch.object(b._session_manager, "add_session", new_callable=AsyncMock, side_effect=Exception("failed")), \
             patch.object(b._session_manager, "aggregate_tools", new_callable=AsyncMock, return_value=[]):
            await b.start()  # Should not raise, logs error
            assert b._initialized is True


class TestMCPBackendSendStream:
    @pytest.mark.asyncio
    async def test_send_not_implemented(self):
        b = MCPBackend()
        with pytest.raises(NotImplementedError):
            await b.send("test")

    @pytest.mark.asyncio
    async def test_stream_not_implemented(self):
        b = MCPBackend()
        with pytest.raises(NotImplementedError):
            await b.stream("test")


class TestMCPBackendSessions:
    @pytest.mark.asyncio
    async def test_create_session_not_implemented(self):
        b = MCPBackend()
        with pytest.raises(NotImplementedError):
            await b.create_session()

    @pytest.mark.asyncio
    async def test_resume_session_not_implemented(self):
        from sdk._types import SessionRef, Backend
        b = MCPBackend()
        ref = SessionRef(session_id="s1", backend=Backend.COPILOT)
        with pytest.raises(NotImplementedError):
            await b.resume_session(ref)

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self):
        b = MCPBackend()
        result = await b.list_sessions()
        assert result == []

    @pytest.mark.asyncio
    async def test_delete_session_not_implemented(self):
        from sdk._types import SessionRef, Backend
        b = MCPBackend()
        ref = SessionRef(session_id="s1", backend=Backend.COPILOT)
        with pytest.raises(NotImplementedError):
            await b.delete_session(ref)


class TestMCPBackendTools:
    def test_register_tool(self):
        b = MCPBackend()
        spec = ToolSpec(name="t1", description="test", parameters={}, handler=lambda: None)
        b.register_tool(spec)
        assert len(b._tools) == 1
        assert len(b.list_tools()) == 1

    def test_list_tools_returns_copy(self):
        b = MCPBackend()
        spec = ToolSpec(name="t1", description="test", parameters={}, handler=lambda: None)
        b.register_tool(spec)
        tools = b.list_tools()
        tools.clear()
        assert len(b._tools) == 1  # Original unchanged

    def test_get_tool_registry(self):
        b = MCPBackend()
        reg = b.get_tool_registry()
        assert reg is not None

    @pytest.mark.asyncio
    async def test_call_tool_not_found(self):
        b = MCPBackend()
        with pytest.raises(MCPError):
            await b.call_tool("nonexistent", {})

    @pytest.mark.asyncio
    async def test_call_tool_success(self):
        b = MCPBackend()

        async def my_handler(**kwargs):
            return {"result": "ok"}

        spec = ToolSpec(name="my_tool", description="test", parameters={}, handler=my_handler)
        b.register_tool(spec)

        result = await b.call_tool("my_tool", {})
        assert result == {"result": "ok"}


class TestMCPBackendHooks:
    def test_register_hook(self):
        b = MCPBackend()
        cb = MagicMock()
        b.register_hook(HookPoint.PRE_TOOL_USE, cb)
        assert cb in b._hooks[HookPoint.PRE_TOOL_USE]

    @pytest.mark.asyncio
    async def test_hooks_run_on_call_tool(self):
        b = MCPBackend()
        pre_hook = MagicMock()
        post_hook = MagicMock()
        b.register_hook(HookPoint.PRE_TOOL_USE, pre_hook)
        b.register_hook(HookPoint.POST_TOOL_USE, post_hook)

        async def my_handler(**kwargs):
            return "ok"

        spec = ToolSpec(name="t1", description="test", parameters={}, handler=my_handler)
        b.register_tool(spec)

        await b.call_tool("t1", {})
        pre_hook.assert_called_once()
        post_hook.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_hooks(self):
        b = MCPBackend()

        async def async_hook(context):
            pass

        b.register_hook(HookPoint.PRE_TOOL_USE, async_hook)

        async def my_handler(**kwargs):
            return "ok"

        spec = ToolSpec(name="t1", description="test", parameters={}, handler=my_handler)
        b.register_tool(spec)

        await b.call_tool("t1", {})  # Should not raise


class TestMCPBackendMCPToolConversion:
    def test_mcp_tool_to_obscura_with_session_prefix(self):
        b = MCPBackend()
        mcp_tool = MagicMock()
        mcp_tool.name = "server1.read_file"
        mcp_tool.description = "Read a file"
        mcp_tool.inputSchema = {"type": "object"}

        result = b._mcp_tool_to_obscura(mcp_tool)
        assert result.name == "server1.read_file"
        assert result.description == "Read a file"

    def test_mcp_tool_to_obscura_no_prefix(self):
        b = MCPBackend()
        mcp_tool = MagicMock()
        mcp_tool.name = "read_file"
        mcp_tool.description = "Read a file"
        mcp_tool.inputSchema = {}

        result = b._mcp_tool_to_obscura(mcp_tool)
        assert result.name == "read_file"


class TestMCPBackendServerManagement:
    def test_list_servers(self):
        b = MCPBackend()
        assert b.list_servers() == []
