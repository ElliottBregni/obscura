"""Tests for sdk.heartbeat.client — AgentHeartbeatClient and HeartbeatClientPool."""

import pytest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from sdk.heartbeat.client import (
    AgentHeartbeatClient,
    HeartbeatClientConfig,
    HeartbeatClientPool,
)
from sdk.heartbeat.types import HealthStatus, Heartbeat, SystemMetrics


class TestHeartbeatClientConfig:
    def test_defaults(self) -> None:
        cfg = HeartbeatClientConfig(agent_id="a1", monitor_url="http://localhost:8080")
        assert cfg.agent_id == "a1"
        assert cfg.interval == 30
        assert cfg.timeout == 10.0
        assert cfg.max_retries == 3
        assert cfg.auto_reconnect is True
        assert cfg.collect_metrics is True

    def test_custom(self) -> None:
        cfg = HeartbeatClientConfig(
            agent_id="a2",
            monitor_url="http://host",
            interval=5,
            timeout=2.0,
            max_retries=1,
        )
        assert cfg.interval == 5
        assert cfg.timeout == 2.0
        assert cfg.max_retries == 1


class TestAgentHeartbeatClientInit:
    def test_init(self) -> None:
        client = AgentHeartbeatClient("agent-1", "http://localhost:8080", interval=10)
        assert client.config.agent_id == "agent-1"
        assert client.config.interval == 10
        assert client.is_running is False
        assert client.is_connected is False
        assert client.uptime_seconds == 0.0

    def test_trailing_slash_stripped(self) -> None:
        client = AgentHeartbeatClient("a1", "http://localhost:8080/")
        assert client.config.monitor_url == "http://localhost:8080"


class TestAgentHeartbeatClientLifecycle:
    @pytest.mark.asyncio
    async def test_start_and_stop(self) -> None:
        client = AgentHeartbeatClient("agent-1", "http://localhost:8080", interval=10)

        with patch.object(
            client, "_send_heartbeat", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True
            await client.start()
            assert client.is_running is True
            assert client.start_time > 0
            mock_send.assert_awaited_once()

            await client.stop()
            assert client.is_running is False
            assert client.http_client is None

    @pytest.mark.asyncio
    async def test_start_already_running(self) -> None:
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        client.set_running_for_testing(True)
        # Should return without error
        await client.start()

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self) -> None:
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        await client.stop()  # Should not raise


class TestAgentHeartbeatClientSend:
    @pytest.mark.asyncio
    async def test_send_heartbeat_success(self) -> None:
        client = AgentHeartbeatClient("a1", "http://localhost:8080", interval=10)
        mock_http: Any = AsyncMock()
        mock_response: Any = MagicMock()
        mock_response.status_code = 200
        mock_http.post.return_value = mock_response
        client.set_http_client_for_testing(mock_http)

        result = await client._send_heartbeat()  # pyright: ignore[reportPrivateUsage]
        assert result is True
        assert client.connected is True
        assert client.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_send_heartbeat_no_client(self) -> None:
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        result = await client._send_heartbeat()  # pyright: ignore[reportPrivateUsage]
        assert result is False

    @pytest.mark.asyncio
    async def test_send_heartbeat_server_error(self) -> None:
        client = AgentHeartbeatClient("a1", "http://localhost:8080", interval=10)
        mock_http: Any = AsyncMock()
        mock_response: Any = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_http.post.return_value = mock_response
        client.set_http_client_for_testing(mock_http)
        client.config = HeartbeatClientConfig(
            agent_id="a1",
            monitor_url="http://localhost:8080",
            max_retries=1,
            retry_delay=0.01,
        )

        result = await client._send_heartbeat()  # pyright: ignore[reportPrivateUsage]
        assert result is False
        assert client.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_send_heartbeat_connection_error(self) -> None:
        import httpx

        client = AgentHeartbeatClient("a1", "http://localhost:8080", interval=10)
        mock_http: Any = AsyncMock()
        mock_http.post.side_effect = httpx.TimeoutException("timeout")
        client.set_http_client_for_testing(mock_http)
        client.config = HeartbeatClientConfig(
            agent_id="a1",
            monitor_url="http://localhost:8080",
            max_retries=1,
            retry_delay=0.01,
        )

        result = await client._send_heartbeat()  # pyright: ignore[reportPrivateUsage]
        assert result is False


class TestAgentHeartbeatClientMetrics:
    @pytest.mark.asyncio
    async def test_collect_metrics_no_psutil(self) -> None:
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        client._psutil_available = False  # pyright: ignore[reportPrivateUsage]
        metrics = await client._collect_metrics()  # pyright: ignore[reportPrivateUsage]
        assert isinstance(metrics, SystemMetrics)
        assert metrics.cpu_percent == 0.0

    @pytest.mark.asyncio
    async def test_collect_metrics_disabled(self) -> None:
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        client.config.include_system_metrics = False
        metrics = await client._collect_metrics()  # pyright: ignore[reportPrivateUsage]
        assert metrics.cpu_percent == 0.0

    @pytest.mark.asyncio
    async def test_build_heartbeat(self) -> None:
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        client._psutil_available = False  # pyright: ignore[reportPrivateUsage]
        hb = await client._build_heartbeat()  # pyright: ignore[reportPrivateUsage]
        assert isinstance(hb, Heartbeat)
        assert hb.agent_id == "a1"
        assert hb.status == HealthStatus.HEALTHY


class TestAgentHeartbeatClientCallbacks:
    def test_on_status_change(self) -> None:
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        cb: Any = MagicMock()
        client.on_status_change(cb)
        assert cb in client._status_callbacks  # pyright: ignore[reportPrivateUsage]

    def test_remove_callback(self) -> None:
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        cb: Any = MagicMock()
        client.on_status_change(cb)
        assert client.remove_callback(cb) is True
        assert client.remove_callback(cb) is False


class TestAgentHeartbeatClientStatus:
    @pytest.mark.asyncio
    async def test_send_status_update_success(self) -> None:
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        client._psutil_available = False  # pyright: ignore[reportPrivateUsage]
        mock_http: Any = AsyncMock()
        mock_response: Any = MagicMock()
        mock_response.status_code = 200
        mock_http.post.return_value = mock_response
        client.set_http_client_for_testing(mock_http)

        result = await client.send_status_update(HealthStatus.HEALTHY, "all good")
        assert result is True

    @pytest.mark.asyncio
    async def test_send_status_update_no_client(self) -> None:
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        result = await client.send_status_update(HealthStatus.WARNING)
        assert result is False

    @pytest.mark.asyncio
    async def test_check_health(self) -> None:
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        mock_http: Any = AsyncMock()
        mock_response: Any = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "healthy"}
        mock_http.get.return_value = mock_response
        client.set_http_client_for_testing(mock_http)

        status = await client.check_health()
        assert status == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_check_health_no_client(self) -> None:
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        status = await client.check_health()
        assert status == HealthStatus.UNKNOWN

    def test_get_stats(self) -> None:
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        stats = client.get_stats()
        assert stats["agent_id"] == "a1"
        assert stats["running"] is False
        assert stats["connected"] is False


class TestHeartbeatClientPool:
    @pytest.mark.asyncio
    async def test_register_and_unregister(self) -> None:
        pool = HeartbeatClientPool()

        with patch.object(AgentHeartbeatClient, "start", new_callable=AsyncMock):
            c = await pool.register("a1", "http://localhost:8080")
            assert isinstance(c, AgentHeartbeatClient)
            assert "a1" in pool.list_clients()

        with patch.object(AgentHeartbeatClient, "stop", new_callable=AsyncMock):
            assert await pool.unregister("a1") is True
            assert "a1" not in pool.list_clients()

    @pytest.mark.asyncio
    async def test_unregister_unknown(self) -> None:
        pool = HeartbeatClientPool()
        assert await pool.unregister("unknown") is False

    def test_get_client(self) -> None:
        pool = HeartbeatClientPool()
        assert pool.get_client("x") is None

    @pytest.mark.asyncio
    async def test_stop_all(self) -> None:
        pool = HeartbeatClientPool()
        with patch.object(AgentHeartbeatClient, "start", new_callable=AsyncMock):
            await pool.register("a1", "http://localhost:8080")
            await pool.register("a2", "http://localhost:8080")

        with patch.object(AgentHeartbeatClient, "stop", new_callable=AsyncMock):
            await pool.stop_all()
            assert len(pool.list_clients()) == 0
