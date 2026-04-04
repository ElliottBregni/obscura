"""Tests for the Unix domain socket A2A transport."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.integrations.a2a.transports.unix_socket import (
    UnixSocketA2AClient,
    start_unix_socket_server,
    stop_unix_socket_server,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def socket_path(tmp_path: object) -> str:
    """Generate a temporary socket path."""
    return os.path.join(tempfile.mkdtemp(), "test-a2a.sock")


def _make_mock_service() -> MagicMock:
    """Create a mock A2AService."""
    service = MagicMock()

    # Mock message_send to return a task-like object
    mock_task = MagicMock()
    mock_task.model_dump_json.return_value = json.dumps(
        {
            "id": "task-123",
            "contextId": "ctx-1",
            "status": {
                "state": "completed",
                "message": {
                    "role": "agent",
                    "messageId": "msg-1",
                    "parts": [{"kind": "text", "text": "Hello from agent"}],
                },
            },
            "artifacts": [
                {
                    "artifactId": "art-1",
                    "parts": [{"kind": "text", "text": "Task result"}],
                },
            ],
            "history": [],
            "kind": "task",
        },
    )
    service.message_send = AsyncMock(return_value=mock_task)

    # Mock tasks_get
    service.tasks_get = AsyncMock(return_value=mock_task)

    # Mock tasks_list
    service.tasks_list = AsyncMock(return_value=([mock_task], None))

    # Mock tasks_cancel
    service.tasks_cancel = AsyncMock(return_value=mock_task)

    # Mock get_agent_card
    mock_card = MagicMock()
    mock_card.model_dump_json.return_value = json.dumps(
        {
            "name": "Test Agent",
            "url": "unix:///tmp/test.sock",
            "version": "1.0",
        },
    )
    service.get_agent_card = MagicMock(return_value=mock_card)

    return service


# ---------------------------------------------------------------------------
# Server tests
# ---------------------------------------------------------------------------


class TestUnixSocketServer:
    @pytest.mark.asyncio
    async def test_start_and_stop(self, socket_path: str) -> None:
        service = _make_mock_service()
        server = await start_unix_socket_server(service, socket_path)

        assert os.path.exists(socket_path)

        await stop_unix_socket_server(server, socket_path)

        assert not os.path.exists(socket_path)

    @pytest.mark.asyncio
    async def test_removes_stale_socket(self, socket_path: str) -> None:
        # Create a stale socket file
        os.makedirs(os.path.dirname(socket_path), exist_ok=True)
        with open(socket_path, "w") as f:
            f.write("stale")

        service = _make_mock_service()
        server = await start_unix_socket_server(service, socket_path)

        assert os.path.exists(socket_path)

        await stop_unix_socket_server(server, socket_path)


# ---------------------------------------------------------------------------
# Client tests
# ---------------------------------------------------------------------------


class TestUnixSocketClient:
    @pytest.mark.asyncio
    async def test_send_message_roundtrip(self, socket_path: str) -> None:
        service = _make_mock_service()
        server = await start_unix_socket_server(service, socket_path)

        try:
            client = UnixSocketA2AClient(socket_path)
            await client.connect()

            result = await client.send_message("Hello agent")
            assert "Task result" in result

            await client.disconnect()
        finally:
            await stop_unix_socket_server(server, socket_path)

    @pytest.mark.asyncio
    async def test_raw_request_get_agent_card(
        self,
        socket_path: str,
    ) -> None:
        service = _make_mock_service()
        server = await start_unix_socket_server(service, socket_path)

        try:
            client = UnixSocketA2AClient(socket_path)
            await client.connect()

            response = await client.raw_request("GetAgentCard")
            assert "result" in response
            assert response["result"]["name"] == "Test Agent"

            await client.disconnect()
        finally:
            await stop_unix_socket_server(server, socket_path)

    @pytest.mark.asyncio
    async def test_unknown_method_returns_error(
        self,
        socket_path: str,
    ) -> None:
        service = _make_mock_service()
        server = await start_unix_socket_server(service, socket_path)

        try:
            client = UnixSocketA2AClient(socket_path)
            await client.connect()

            response = await client.raw_request("NonExistentMethod")
            assert "error" in response
            assert response["error"]["code"] == -32601

            await client.disconnect()
        finally:
            await stop_unix_socket_server(server, socket_path)

    @pytest.mark.asyncio
    async def test_connect_without_server_raises(self) -> None:
        client = UnixSocketA2AClient("/tmp/nonexistent-test.sock")
        with pytest.raises((ConnectionRefusedError, FileNotFoundError)):
            await client.connect()

    @pytest.mark.asyncio
    async def test_send_without_connect_raises(self) -> None:
        client = UnixSocketA2AClient("/tmp/any.sock")
        with pytest.raises(RuntimeError, match="Not connected"):
            await client.send_message("hello")
