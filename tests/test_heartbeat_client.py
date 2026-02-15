"""Tests for sdk.heartbeat.client — AgentHeartbeatClient and HeartbeatClientPool."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from sdk.heartbeat.client import (
    AgentHeartbeatClient,
    HeartbeatClientConfig,
    HeartbeatClientPool,
)
from sdk.heartbeat.types import HealthStatus, Heartbeat, SystemMetrics


class TestHeartbeatClientConfig:
    def test_defaults(self):
        cfg = HeartbeatClientConfig(agent_id="a1", monitor_url="http://localhost:8080")
        assert cfg.agent_id == "a1"
        assert cfg.interval == 30
        assert cfg.timeout == 10.0
        assert cfg.max_retries == 3
        assert cfg.auto_reconnect is True
        assert cfg.collect_metrics is True

    def test_custom(self):
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
    def test_init(self):
        client = AgentHeartbeatClient("agent-1", "http://localhost:8080", interval=10)
        assert client.config.agent_id == "agent-1"
        assert client.config.interval == 10
        assert client.is_running is False
        assert client.is_connected is False
        assert client.uptime_seconds == 0.0

    def test_trailing_slash_stripped(self):
        client = AgentHeartbeatClient("a1", "http://localhost:8080/")
        assert client.config.monitor_url == "http://localhost:8080"


class TestAgentHeartbeatClientLifecycle:
    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        client = AgentHeartbeatClient("agent-1", "http://localhost:8080", interval=10)

        with patch.object(client, "_send_heartbeat", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            await client.start()
            assert client.is_running is True
            assert client._start_time > 0
            mock_send.assert_awaited_once()

            await client.stop()
            assert client.is_running is False
            assert client._http_client is None

    @pytest.mark.asyncio
    async def test_start_already_running(self):
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        client._running = True
        # Should return without error
        await client.start()

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self):
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        await client.stop()  # Should not raise


class TestAgentHeartbeatClientSend:
    @pytest.mark.asyncio
    async def test_send_heartbeat_success(self):
        client = AgentHeartbeatClient("a1", "http://localhost:8080", interval=10)
        mock_http = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_http.post.return_value = mock_response
        client._http_client = mock_http

        result = await client._send_heartbeat()
        assert result is True
        assert client._connected is True
        assert client._consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_send_heartbeat_no_client(self):
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        result = await client._send_heartbeat()
        assert result is False

    @pytest.mark.asyncio
    async def test_send_heartbeat_server_error(self):
        client = AgentHeartbeatClient("a1", "http://localhost:8080", interval=10)
        mock_http = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_http.post.return_value = mock_response
        client._http_client = mock_http
        client.config = HeartbeatClientConfig(
            agent_id="a1", monitor_url="http://localhost:8080",
            max_retries=1, retry_delay=0.01,
        )

        result = await client._send_heartbeat()
        assert result is False
        assert client._consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_send_heartbeat_connection_error(self):
        import httpx
        client = AgentHeartbeatClient("a1", "http://localhost:8080", interval=10)
        mock_http = AsyncMock()
        mock_http.post.side_effect = httpx.TimeoutException("timeout")
        client._http_client = mock_http
        client.config = HeartbeatClientConfig(
            agent_id="a1", monitor_url="http://localhost:8080",
            max_retries=1, retry_delay=0.01,
        )

        result = await client._send_heartbeat()
        assert result is False


class TestAgentHeartbeatClientMetrics:
    @pytest.mark.asyncio
    async def test_collect_metrics_no_psutil(self):
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        client._psutil_available = False
        metrics = await client._collect_metrics()
        assert isinstance(metrics, SystemMetrics)
        assert metrics.cpu_percent == 0.0

    @pytest.mark.asyncio
    async def test_collect_metrics_disabled(self):
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        client.config.include_system_metrics = False
        metrics = await client._collect_metrics()
        assert metrics.cpu_percent == 0.0

    @pytest.mark.asyncio
    async def test_build_heartbeat(self):
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        client._psutil_available = False
        hb = await client._build_heartbeat()
        assert isinstance(hb, Heartbeat)
        assert hb.agent_id == "a1"
        assert hb.status == HealthStatus.HEALTHY


class TestAgentHeartbeatClientCallbacks:
    def test_on_status_change(self):
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        cb = MagicMock()
        client.on_status_change(cb)
        assert cb in client._status_callbacks

    def test_remove_callback(self):
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        cb = MagicMock()
        client.on_status_change(cb)
        assert client.remove_callback(cb) is True
        assert client.remove_callback(cb) is False


class TestAgentHeartbeatClientStatus:
    @pytest.mark.asyncio
    async def test_send_status_update_success(self):
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        client._psutil_available = False
        mock_http = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_http.post.return_value = mock_response
        client._http_client = mock_http

        result = await client.send_status_update(HealthStatus.HEALTHY, "all good")
        assert result is True

    @pytest.mark.asyncio
    async def test_send_status_update_no_client(self):
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        result = await client.send_status_update(HealthStatus.WARNING)
        assert result is False

    @pytest.mark.asyncio
    async def test_check_health(self):
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        mock_http = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "healthy"}
        mock_http.get.return_value = mock_response
        client._http_client = mock_http

        status = await client.check_health()
        assert status == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_check_health_no_client(self):
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        status = await client.check_health()
        assert status == HealthStatus.UNKNOWN

    def test_get_stats(self):
        client = AgentHeartbeatClient("a1", "http://localhost:8080")
        stats = client.get_stats()
        assert stats["agent_id"] == "a1"
        assert stats["running"] is False
        assert stats["connected"] is False


class TestHeartbeatClientPool:
    @pytest.mark.asyncio
    async def test_register_and_unregister(self):
        pool = HeartbeatClientPool()

        with patch.object(AgentHeartbeatClient, "start", new_callable=AsyncMock):
            c = await pool.register("a1", "http://localhost:8080")
            assert isinstance(c, AgentHeartbeatClient)
            assert "a1" in pool.list_clients()

        with patch.object(AgentHeartbeatClient, "stop", new_callable=AsyncMock):
            assert await pool.unregister("a1") is True
            assert "a1" not in pool.list_clients()

    @pytest.mark.asyncio
    async def test_unregister_unknown(self):
        pool = HeartbeatClientPool()
        assert await pool.unregister("unknown") is False

    def test_get_client(self):
        pool = HeartbeatClientPool()
        assert pool.get_client("x") is None

    @pytest.mark.asyncio
    async def test_stop_all(self):
        pool = HeartbeatClientPool()
        with patch.object(AgentHeartbeatClient, "start", new_callable=AsyncMock):
            await pool.register("a1", "http://localhost:8080")
            await pool.register("a2", "http://localhost:8080")

        with patch.object(AgentHeartbeatClient, "stop", new_callable=AsyncMock):
            await pool.stop_all()
            assert len(pool.list_clients()) == 0
