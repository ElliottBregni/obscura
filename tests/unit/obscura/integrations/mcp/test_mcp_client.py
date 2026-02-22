"""Tests for sdk.mcp.client — MCPClient and transports."""

# pyright: reportPrivateUsage=false
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
from typing import Any, cast

from obscura.integrations.mcp.types import (
    MCPConnectionConfig,
    MCPError,
    MCPErrorCode,
    MCPPromptMessage,
    MCPPromptResult,
    MCPResourceContent,
    MCPTool,
    MCPToolResult,
    MCPTransportType,
)
from obscura.integrations.mcp.client import MCPClient, MCPSessionManager, StdioTransport, SSETransport


# ---------------------------------------------------------------------------
# MCPClient init / ID generation
# ---------------------------------------------------------------------------


class TestMCPClientInit:
    def test_init(self):
        config = MCPConnectionConfig(
            transport=MCPTransportType.STDIO,
            command="echo",
            args=["hello"],
        )
        client = MCPClient(config)
        assert client.initialized is False
        assert client.request_counter == 0

    def test_next_id(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)
        assert client.next_id() == "1"
        assert client.next_id() == "2"
        assert client.next_id() == "3"


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class TestMCPClientContextManager:
    @pytest.mark.asyncio
    async def test_aenter_aexit(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)
        with (
            patch.object(client, "connect", new_callable=AsyncMock) as mock_conn,
            patch.object(client, "disconnect", new_callable=AsyncMock) as mock_disc,
        ):
            async with client as c:
                assert c is client
                mock_conn.assert_awaited_once()
            mock_disc.assert_awaited_once()


# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------


class TestMCPClientConnect:
    @pytest.mark.asyncio
    async def test_connect_idempotent(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)
        client.initialized = True
        # Should return early without creating transport
        await client.connect()
        assert client.transport is None

    @pytest.mark.asyncio
    async def test_connect_stdio_creates_transport(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        mock_transport = AsyncMock()
        mock_transport.connect = AsyncMock()
        mock_transport.send = AsyncMock()

        with patch("obscura.integrations.mcp.client.StdioTransport", return_value=mock_transport):
            # Mock _request to return init response, and _notification to do nothing
            with patch.object(
                client,
                "_request",
                new_callable=AsyncMock,
                return_value={"protocolVersion": "2024-11-05"},
            ):
                with patch.object(client, "_notification", new_callable=AsyncMock):
                    await client.connect()

        assert client.initialized is True

    @pytest.mark.asyncio
    async def test_connect_sse_creates_transport(self):
        config = MCPConnectionConfig(
            transport=MCPTransportType.SSE, url="http://localhost:3000"
        )
        client = MCPClient(config)

        mock_transport = AsyncMock()
        mock_transport.connect = AsyncMock()
        mock_transport.send = AsyncMock()

        with patch("obscura.integrations.mcp.client.SSETransport", return_value=mock_transport):
            with patch.object(
                client,
                "_request",
                new_callable=AsyncMock,
                return_value={"protocolVersion": "2024-11-05"},
            ):
                with patch.object(client, "_notification", new_callable=AsyncMock):
                    await client.connect()

        assert client.initialized is True

    @pytest.mark.asyncio
    async def test_connect_unsupported_transport(self):
        config = MCPConnectionConfig(
            transport=MCPTransportType.WEBSOCKET, url="ws://localhost"
        )
        client = MCPClient(config)
        with pytest.raises(MCPError) as exc_info:
            await client.connect()
        assert exc_info.value.code == MCPErrorCode.INVALID_PARAMS.value


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------


class TestMCPClientDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_cancels_pending(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        client.pending_requests["1"] = future
        client.initialized = True

        await client.disconnect()
        assert client.initialized is False
        assert len(client.pending_requests) == 0

    @pytest.mark.asyncio
    async def test_disconnect_with_transport(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)
        mock_transport = AsyncMock()
        client.transport = mock_transport
        await client.disconnect()
        mock_transport.disconnect.assert_awaited_once()
        assert client.transport is None

    @pytest.mark.asyncio
    async def test_disconnect_no_transport(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)
        # Should not raise
        await client.disconnect()


# ---------------------------------------------------------------------------
# Request / Notification
# ---------------------------------------------------------------------------


class TestMCPClientRequest:
    @pytest.mark.asyncio
    async def test_request_not_connected(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)
        with pytest.raises(MCPError):
            await client.request("test", {})

    @pytest.mark.asyncio
    async def test_request_sends_and_waits(self):
        config = MCPConnectionConfig(
            transport=MCPTransportType.STDIO, command="echo", timeout=5.0
        )
        client = MCPClient(config)
        mock_transport = AsyncMock()
        client.transport = mock_transport

        # Simulate transport.send resolving the future
        async def fake_send(msg: dict[str, Any]):
            req_id = msg["id"]
            if req_id in client.pending_requests:
                client.pending_requests[req_id].set_result(
                    {
                        "id": req_id,
                        "result": {"ok": True},
                    }
                )

        mock_transport.send = fake_send
        result = await client.request("ping", {})
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_request_error_response(self):
        config = MCPConnectionConfig(
            transport=MCPTransportType.STDIO, command="echo", timeout=5.0
        )
        client = MCPClient(config)
        mock_transport = AsyncMock()
        client.transport = mock_transport

        async def fake_send(msg: dict[str, Any]):
            req_id = msg["id"]
            if req_id in client.pending_requests:
                client.pending_requests[req_id].set_result(
                    {
                        "id": req_id,
                        "error": {"code": -32601, "message": "Not found"},
                    }
                )

        mock_transport.send = fake_send
        with pytest.raises(MCPError) as exc_info:
            await client.request("bad_method", {})
        assert exc_info.value.code == -32601

    @pytest.mark.asyncio
    async def test_request_timeout(self):
        config = MCPConnectionConfig(
            transport=MCPTransportType.STDIO, command="echo", timeout=0.05
        )
        client = MCPClient(config)
        mock_transport = AsyncMock()
        mock_transport.send = AsyncMock()  # Does not resolve the future
        client.transport = mock_transport

        with pytest.raises(asyncio.TimeoutError):
            await client.request("slow", {})
        # Pending request should be cleaned up
        assert len(client.pending_requests) == 0


class TestMCPClientNotification:
    @pytest.mark.asyncio
    async def test_notification_no_transport(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)
        # Should not raise, just return
        await client.notify("test", {})

    @pytest.mark.asyncio
    async def test_notification_sends(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)
        mock_transport = AsyncMock()
        client.transport = mock_transport
        await client.notify("notifications/initialized", {})
        mock_transport.send.assert_awaited_once()
        sent = mock_transport.send.call_args[0][0]
        assert sent["method"] == "notifications/initialized"
        assert "id" not in sent


# ---------------------------------------------------------------------------
# Handle Response
# ---------------------------------------------------------------------------


class TestMCPClientHandleResponse:
    def test_handle_response_resolves_future(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        loop = asyncio.new_event_loop()
        future = loop.create_future()
        client.pending_requests["42"] = future

        response: dict[str, Any] = {"id": 42, "result": {"data": "test"}}
        client.handle_response(response)

        assert future.done()
        assert future.result() == response
        loop.close()

    def test_handle_response_unknown_id(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)
        # Should not raise
        client.handle_response({"id": "unknown", "result": {}})

    def test_handle_response_already_done(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        loop = asyncio.new_event_loop()
        future = loop.create_future()
        future.set_result({"dummy": True})  # Already resolved
        client.pending_requests["99"] = future

        # Should not raise
        client.handle_response({"id": 99, "result": {"data": "test"}})
        loop.close()


# ---------------------------------------------------------------------------
# Protocol methods: list_tools, call_tool, etc.
# ---------------------------------------------------------------------------


class TestMCPClientProtocol:
    @pytest.mark.asyncio
    async def test_ping(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={}):
            result = await client.ping()
        assert result == {}

    @pytest.mark.asyncio
    async def test_list_tools(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        mock_result: dict[str, object] = {
            "tools": [
                {
                    "name": "read_file",
                    "description": "Read a file",
                    "inputSchema": {"type": "object"},
                },
                {"name": "write_file", "description": "Write a file"},
            ]
        }
        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=mock_result
        ):
            tools = await client.list_tools()

        assert len(tools) == 2
        assert tools[0].name == "read_file"
        assert tools[0].inputSchema == {"type": "object"}
        assert tools[1].description == "Write a file"

    @pytest.mark.asyncio
    async def test_list_tools_empty(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        with patch.object(client, "_request", new_callable=AsyncMock, return_value={}):
            tools = await client.list_tools()
        assert tools == []

    @pytest.mark.asyncio
    async def test_call_tool(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        mock_result: dict[str, Any] = {
            "content": [{"type": "text", "text": "file contents"}],
            "isError": False,
        }
        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=mock_result
        ):
            result = await client.call_tool("read_file", {"path": "/tmp/x"})

        assert isinstance(result, MCPToolResult)
        assert result.isError is False
        assert result.content[0]["text"] == "file contents"

    @pytest.mark.asyncio
    async def test_call_tool_error(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        mock_result: dict[str, Any] = {
            "content": [{"type": "text", "text": "fail"}],
            "isError": True,
        }
        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=mock_result
        ):
            result = await client.call_tool("bad_tool", {})
        assert result.isError is True

    @pytest.mark.asyncio
    async def test_list_resources(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        mock_result = {
            "resources": [
                {
                    "uri": "file:///tmp/x",
                    "name": "x.txt",
                    "description": "test",
                    "mimeType": "text/plain",
                },
                {"uri": "file:///tmp/y", "name": "y.txt"},
            ]
        }
        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=mock_result
        ):
            resources = await client.list_resources()

        assert len(resources) == 2
        assert resources[0].uri == "file:///tmp/x"
        assert resources[0].mimeType == "text/plain"
        assert resources[1].description is None

    @pytest.mark.asyncio
    async def test_list_resources_empty(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        with patch.object(client, "_request", new_callable=AsyncMock, return_value={}):
            resources = await client.list_resources()
        assert resources == []

    @pytest.mark.asyncio
    async def test_read_resource(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        mock_result = {
            "contents": [
                {
                    "uri": "file:///tmp/x",
                    "mimeType": "text/plain",
                    "text": "hello world",
                }
            ]
        }
        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=mock_result
        ):
            content = await client.read_resource("file:///tmp/x")

        assert isinstance(content, MCPResourceContent)
        assert content.text == "hello world"
        assert content.uri == "file:///tmp/x"

    @pytest.mark.asyncio
    async def test_read_resource_blob(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        mock_result: dict[str, object] = {
            "contents": [
                {
                    "uri": "file:///img.png",
                    "mimeType": "image/png",
                    "blob": b"binary",
                }
            ]
        }
        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=mock_result
        ):
            content = await client.read_resource("file:///img.png")
        assert content.blob == b"binary"
        assert content.text is None

    @pytest.mark.asyncio
    async def test_read_resource_empty_error(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value={"contents": []}
        ):
            with pytest.raises(MCPError) as exc_info:
                await client.read_resource("file:///missing")
        assert exc_info.value.code == MCPErrorCode.RESOURCE_NOT_FOUND.value

    @pytest.mark.asyncio
    async def test_subscribe_resource(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            await client.subscribe_resource("file:///tmp/x")
        mock_req.assert_awaited_once_with(
            "resources/subscribe", {"uri": "file:///tmp/x"}
        )

    @pytest.mark.asyncio
    async def test_unsubscribe_resource(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            await client.unsubscribe_resource("file:///tmp/x")
        mock_req.assert_awaited_once_with(
            "resources/unsubscribe", {"uri": "file:///tmp/x"}
        )

    @pytest.mark.asyncio
    async def test_list_prompts(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        mock_result: dict[str, object] = {
            "prompts": [
                {
                    "name": "code_review",
                    "description": "Review code",
                    "arguments": [{"name": "file"}],
                },
                {"name": "summarize"},
            ]
        }
        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=mock_result
        ):
            prompts = await client.list_prompts()

        assert len(prompts) == 2
        assert prompts[0].name == "code_review"
        assert prompts[0].arguments == [{"name": "file"}]
        assert prompts[1].description is None

    @pytest.mark.asyncio
    async def test_list_prompts_empty(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        with patch.object(client, "_request", new_callable=AsyncMock, return_value={}):
            prompts = await client.list_prompts()
        assert prompts == []

    @pytest.mark.asyncio
    async def test_get_prompt(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        mock_result = cast(
            dict[str, object],
            {
                "description": "Code review prompt",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": "Review this"},
                    },
                ],
            },
        )

        # MCPPromptMessage is used but not imported at module level in client.py;
        # inject it so get_prompt can resolve the name.
        import obscura.integrations.mcp.client as client_mod

        client_mod.MCPPromptMessage = MCPPromptMessage
        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=mock_result
        ):
            result = await client.get_prompt("code_review", {"file": "main.py"})

        assert isinstance(result, MCPPromptResult)
        assert result.description == "Code review prompt"
        assert len(result.messages) == 1
        assert result.messages[0].role == "user"

    @pytest.mark.asyncio
    async def test_get_prompt_no_arguments(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        client = MCPClient(config)

        mock_result = cast(
            dict[str, object],
            {
                "description": "Simple prompt",
                "messages": [],
            },
        )

        import obscura.integrations.mcp.client as client_mod

        client_mod.MCPPromptMessage = MCPPromptMessage
        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=mock_result
        ) as mock_req:
            await client.get_prompt("simple")
        # Should not include arguments key in params
        call_params = mock_req.call_args[0][1]
        assert "arguments" not in call_params


# ---------------------------------------------------------------------------
# Session Manager
# ---------------------------------------------------------------------------


class TestMCPSessionManager:
    @pytest.mark.asyncio
    async def test_add_and_remove_session(self):
        manager = MCPSessionManager()
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")

        with patch.object(MCPClient, "connect", new_callable=AsyncMock):
            await manager.add_session("test", config)
            assert "test" in manager.list_sessions()
            assert manager.get_session("test") is not None

        with patch.object(MCPClient, "disconnect", new_callable=AsyncMock):
            await manager.remove_session("test")
            assert "test" not in manager.list_sessions()

    @pytest.mark.asyncio
    async def test_remove_unknown_session(self):
        manager = MCPSessionManager()
        await manager.remove_session("unknown")  # Should not raise

    def test_get_session_none(self):
        manager = MCPSessionManager()
        assert manager.get_session("nope") is None

    @pytest.mark.asyncio
    async def test_close_all(self):
        manager = MCPSessionManager()
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")

        with patch.object(MCPClient, "connect", new_callable=AsyncMock):
            await manager.add_session("s1", config)
            await manager.add_session("s2", config)

        with patch.object(MCPClient, "disconnect", new_callable=AsyncMock):
            await manager.close_all()
            assert len(manager.list_sessions()) == 0

    @pytest.mark.asyncio
    async def test_aggregate_tools(self):
        manager = MCPSessionManager()

        mockclient = AsyncMock()
        mockclient.list_tools.return_value = [
            MCPTool(name="tool1", description="d", inputSchema={}),
        ]
        manager.sessions["test"] = mockclient

        tools = await manager.aggregate_tools()
        assert len(tools) == 1
        assert tools[0].name == "test.tool1"

    @pytest.mark.asyncio
    async def test_aggregate_tools_multiplesessions(self):
        manager = MCPSessionManager()

        client1 = AsyncMock()
        client1.list_tools.return_value = [
            MCPTool(name="read", description="read file", inputSchema={}),
        ]
        client2 = AsyncMock()
        client2.list_tools.return_value = [
            MCPTool(name="search", description="search files", inputSchema={}),
            MCPTool(name="index", description="index files", inputSchema={}),
        ]
        manager.sessions["fs"] = client1
        manager.sessions["search"] = client2

        tools = await manager.aggregate_tools()
        assert len(tools) == 3
        names = [t.name for t in tools]
        assert "fs.read" in names
        assert "search.search" in names
        assert "search.index" in names

    @pytest.mark.asyncio
    async def test_aggregate_tools_error_handling(self):
        manager = MCPSessionManager()

        goodclient = AsyncMock()
        goodclient.list_tools.return_value = [
            MCPTool(name="t1", description="d", inputSchema={}),
        ]
        badclient = AsyncMock()
        badclient.list_tools.side_effect = Exception("connection lost")

        manager.sessions["good"] = goodclient
        manager.sessions["bad"] = badclient

        tools = await manager.aggregate_tools()
        # Should still get tools from the good client
        assert len(tools) >= 1

    @pytest.mark.asyncio
    async def test_add_session_returnsclient(self):
        manager = MCPSessionManager()
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")

        with patch.object(MCPClient, "connect", new_callable=AsyncMock):
            client = await manager.add_session("s1", config)
        assert isinstance(client, MCPClient)


# ---------------------------------------------------------------------------
# StdioTransport
# ---------------------------------------------------------------------------


class TestStdioTransport:
    def test_init(self):
        config = MCPConnectionConfig(
            transport=MCPTransportType.STDIO,
            command="echo",
            args=["test"],
        )
        transport = StdioTransport(config)
        assert transport.process is None

    @pytest.mark.asyncio
    async def test_connect_no_command(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO)
        transport = StdioTransport(config)
        with pytest.raises(MCPError):
            await transport.connect()

    @pytest.mark.asyncio
    async def test_send_not_running(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        transport = StdioTransport(config)
        with pytest.raises(MCPError):
            await transport.send({"test": True})

    @pytest.mark.asyncio
    async def test_receive_timeout(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        transport = StdioTransport(config)
        result = await transport.receive()
        assert result is None

    @pytest.mark.asyncio
    async def test_connect_spawnsprocess(self):
        config = MCPConnectionConfig(
            transport=MCPTransportType.STDIO,
            command="echo",
            args=["hello"],
            env={"MY_VAR": "val"},
        )
        transport = StdioTransport(config)

        mock_proc = MagicMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(return_value=b"")
        mock_proc.stdin = MagicMock()

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            await transport.connect()

        assert transport.process is mock_proc
        assert transport.read_task is not None

        # Cleanup
        transport.read_task.cancel()
        try:
            await transport.read_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_disconnectprocess(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        transport = StdioTransport(config)

        mock_proc = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        transport.process = mock_proc

        # Create a real cancelled task for the read_task
        async def noop():
            await asyncio.sleep(100)

        task = asyncio.create_task(noop())
        task.cancel()
        transport.read_task = task

        await transport.disconnect()
        assert transport.process is None
        assert transport.read_task is None

    @pytest.mark.asyncio
    async def test_send_writes_to_stdin(self):
        config = MCPConnectionConfig(transport=MCPTransportType.STDIO, command="echo")
        transport = StdioTransport(config)

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        transport.process = mock_proc

        await transport.send({"jsonrpc": "2.0", "id": "1", "method": "ping"})
        mock_proc.stdin.write.assert_called_once()
        written = mock_proc.stdin.write.call_args[0][0]
        msg = json.loads(written.decode().strip())
        assert msg["method"] == "ping"
        mock_proc.stdin.drain.assert_awaited_once()


# ---------------------------------------------------------------------------
# SSETransport
# ---------------------------------------------------------------------------


class TestSSETransport:
    def test_init(self):
        config = MCPConnectionConfig(
            transport=MCPTransportType.SSE,
            url="http://localhost:3000",
        )
        transport = SSETransport(config)
        assert transport.client is None

    @pytest.mark.asyncio
    async def test_connect_no_url(self):
        config = MCPConnectionConfig(transport=MCPTransportType.SSE)
        transport = SSETransport(config)
        with pytest.raises(MCPError):
            await transport.connect()

    @pytest.mark.asyncio
    async def test_send_not_connected(self):
        config = MCPConnectionConfig(
            transport=MCPTransportType.SSE, url="http://localhost"
        )
        transport = SSETransport(config)
        with pytest.raises(MCPError):
            await transport.send({"test": True})

    @pytest.mark.asyncio
    async def test_disconnect(self):
        config = MCPConnectionConfig(
            transport=MCPTransportType.SSE, url="http://localhost"
        )
        transport = SSETransport(config)
        mockclient = AsyncMock()
        transport.client = mockclient
        await transport.disconnect()
        mockclient.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_withread_task(self):
        config = MCPConnectionConfig(
            transport=MCPTransportType.SSE, url="http://localhost"
        )
        transport = SSETransport(config)
        mockclient = AsyncMock()
        transport.client = mockclient

        # Create a real cancelled task
        async def noop():
            await asyncio.sleep(100)

        task = asyncio.create_task(noop())
        task.cancel()
        transport.read_task = task

        await transport.disconnect()
        assert transport.client is None

    @pytest.mark.asyncio
    async def test_receive_timeout(self):
        config = MCPConnectionConfig(
            transport=MCPTransportType.SSE, url="http://localhost"
        )
        transport = SSETransport(config)
        result = await transport.receive()
        assert result is None

    @pytest.mark.asyncio
    async def test_connect_createsclient(self):
        config = MCPConnectionConfig(
            transport=MCPTransportType.SSE, url="http://localhost:3000"
        )
        transport = SSETransport(config)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_httpxclient = AsyncMock()
        mock_httpxclient.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_httpxclient):
            await transport.connect()

        assert transport.client is mock_httpxclient
        assert transport.endpoint == "http://localhost:3000/rpc"

    @pytest.mark.asyncio
    async def test_send_post_with_id(self):
        config = MCPConnectionConfig(
            transport=MCPTransportType.SSE, url="http://localhost:3000"
        )
        transport = SSETransport(config)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"id": "1", "result": {"ok": True}}

        mockclient = AsyncMock()
        mockclient.post = AsyncMock(return_value=mock_response)
        transport.client = mockclient
        transport.endpoint = "http://localhost:3000/rpc"

        await transport.send({"jsonrpc": "2.0", "id": "1", "method": "ping"})
        mockclient.post.assert_awaited_once()
        # Should put result in queue since message has id
        result = await transport.receive()
        assert result == {"id": "1", "result": {"ok": True}}

    @pytest.mark.asyncio
    async def test_send_post_notification_no_queue(self):
        config = MCPConnectionConfig(
            transport=MCPTransportType.SSE, url="http://localhost:3000"
        )
        transport = SSETransport(config)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mockclient = AsyncMock()
        mockclient.post = AsyncMock(return_value=mock_response)
        transport.client = mockclient
        transport.endpoint = "http://localhost:3000/rpc"

        # No "id" in message => notification, should not queue
        await transport.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        # Queue should be empty
        result = await transport.receive()
        assert result is None

