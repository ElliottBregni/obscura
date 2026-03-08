"""
obscura.heartbeat.client — Agent heartbeat client.

Provides AgentHeartbeatClient for agents to send periodic heartbeats
to a monitoring service.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Optional, Callable

from pydantic import BaseModel

import httpx

from obscura.heartbeat.types import Heartbeat, HealthStatus, SystemMetrics

logger = logging.getLogger(__name__)


class HeartbeatClientConfig(BaseModel):
    """Configuration for the heartbeat client."""

    agent_id: str
    monitor_url: str
    interval: int = 30  # seconds between heartbeats
    timeout: float = 10.0  # HTTP request timeout
    max_retries: int = 3
    retry_delay: float = 5.0  # seconds between retries
    auto_reconnect: bool = True
    reconnect_delay: float = 1.0  # seconds between reconnection attempts
    collect_metrics: bool = True
    include_system_metrics: bool = True
    tags: list[str] = []
    version: str = "0.1.0"
    auth_token: str | None = None


class AgentHeartbeatClient:
    """
    Client for agents to send heartbeats to a monitor.

    This client:
    - Sends periodic heartbeats to the monitor
    - Includes system metrics (CPU, memory via psutil)
    - Auto-reconnects on failure
    - Tracks connection state

    Usage:
        client = AgentHeartbeatClient(
            agent_id="agent-123",
            monitor_url="http://localhost:8080",
            interval=30
        )
        await client.start()

        # Heartbeats are sent automatically
        # ... agent does work ...

        await client.stop()
    """

    def __init__(
        self,
        agent_id: str,
        monitor_url: str,
        interval: int = 30,
        **kwargs: Any,
    ) -> None:
        """
        Initialize the heartbeat client.

        Args:
            agent_id: Unique identifier for this agent
            monitor_url: Base URL of the heartbeat monitor API
            interval: Seconds between heartbeats
            **kwargs: Additional configuration options
        """
        config_fields = set(HeartbeatClientConfig.model_fields.keys())
        filtered_kwargs = {k: v for k, v in kwargs.items() if k in config_fields}

        self.config = HeartbeatClientConfig(
            agent_id=agent_id,
            monitor_url=monitor_url.rstrip("/"),
            interval=interval,
            **filtered_kwargs,
        )

        self._running = False
        self._heartbeat_task: Optional[asyncio.Task[None]] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._start_time: float = 0.0
        self._last_heartbeat_time: Optional[float] = None
        self._consecutive_failures: int = 0
        self._connected: bool = False
        self._status_callbacks: list[Callable[[HealthStatus], None]] = []
        self._current_status: HealthStatus = HealthStatus.UNKNOWN

        # psutil availability
        self._psutil_available = False
        try:
            __import__("psutil")
            self._psutil_available = True
            logger.debug("psutil available for system metrics collection")
        except ImportError:
            logger.warning("psutil not available, system metrics disabled")

    @property
    def is_running(self) -> bool:
        """Check if the heartbeat client is running."""
        return self._running

    @property
    def is_connected(self) -> bool:
        """Check if connected to the monitor."""
        return self._connected

    @property
    def uptime_seconds(self) -> float:
        """Get the uptime of this client."""
        if not self._start_time:
            return 0.0
        return time.time() - self._start_time

    @property
    def start_time(self) -> float:
        return self._start_time

    @property
    def http_client(self) -> httpx.AsyncClient | None:
        return self._http_client

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def connected(self) -> bool:
        return self._connected

    def set_running_for_testing(self, running: bool) -> None:
        self._running = running

    def set_http_client_for_testing(self, client: Any) -> None:
        self._http_client = client

    def set_connected_for_testing(self, connected: bool) -> None:
        self._connected = connected

    async def start(self) -> None:
        """Start the heartbeat client."""
        if self._running:
            logger.warning(
                f"HeartbeatClient for {self.config.agent_id} already running"
            )
            return

        self._running = True
        self._start_time = time.time()
        headers: dict[str, str] = {}
        if self.config.auth_token and self.config.auth_token.strip():
            headers["Authorization"] = f"Bearer {self.config.auth_token}"
        self._http_client = httpx.AsyncClient(
            timeout=self.config.timeout,
            headers=headers,
        )

        # Send initial heartbeat immediately
        await self._send_heartbeat()

        # Start heartbeat loop
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(f"HeartbeatClient started for agent {self.config.agent_id}")

    async def stop(self) -> None:
        """Stop the heartbeat client."""
        if not self._running:
            return

        self._running = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._connected = False
        logger.info(f"HeartbeatClient stopped for agent {self.config.agent_id}")

    async def _heartbeat_loop(self) -> None:
        """Main heartbeat loop."""
        while self._running:
            try:
                await asyncio.sleep(self.config.interval)

                if not self._running:
                    break

                await self._send_heartbeat()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Error in heartbeat loop: {e}")

    async def _send_heartbeat(self) -> bool:
        """
        Send a heartbeat to the monitor.

        Returns True if successful, False otherwise.
        """
        if not self._http_client:
            return False

        # Build heartbeat
        heartbeat = await self._build_heartbeat()

        # Try to send with retries
        url = f"{self.config.monitor_url}/api/v1/heartbeat"

        for attempt in range(self.config.max_retries):
            try:
                response = await self._http_client.post(
                    url,
                    json=heartbeat.to_dict(),
                )

                if response.status_code < 400:
                    self._last_heartbeat_time = time.time()
                    self._consecutive_failures = 0

                    if not self._connected:
                        self._connected = True
                        logger.info(
                            f"Connected to heartbeat monitor for {self.config.agent_id}"
                        )

                    logger.debug(
                        f"Heartbeat sent successfully for {self.config.agent_id}"
                    )
                    return True
                else:
                    logger.warning(
                        f"Heartbeat returned {response.status_code}: {response.text}"
                    )

            except httpx.TimeoutException:
                logger.warning(
                    f"Heartbeat timeout (attempt {attempt + 1}/{self.config.max_retries})"
                )
            except Exception as e:
                logger.warning(
                    f"Heartbeat error (attempt {attempt + 1}/{self.config.max_retries}): {e}"
                )

            # Wait before retry
            if attempt < self.config.max_retries - 1:
                await asyncio.sleep(self.config.retry_delay * (attempt + 1))

        # All retries failed
        self._consecutive_failures += 1

        if self._connected:
            self._connected = False
            logger.warning(
                f"Lost connection to heartbeat monitor for {self.config.agent_id} "
                f"({self._consecutive_failures} consecutive failures)"
            )

        # Trigger auto-reconnect if enabled
        if self.config.auto_reconnect and self._running:
            logger.debug(f"Will retry heartbeat in {self.config.reconnect_delay}s")

        return False

    async def _build_heartbeat(self) -> Heartbeat:
        """Build a heartbeat message."""
        # Collect system metrics
        metrics = await self._collect_metrics()

        # Determine status based on connection state
        if self._consecutive_failures == 0:
            status = HealthStatus.HEALTHY
        elif self._consecutive_failures < 3:
            status = HealthStatus.WARNING
        else:
            status = HealthStatus.CRITICAL

        # Build message
        message = None
        if not self._connected:
            message = (
                f"Disconnected from monitor ({self._consecutive_failures} failures)"
            )

        return Heartbeat(
            agent_id=self.config.agent_id,
            timestamp=datetime.now(),
            status=status,
            metrics=metrics,
            message=message,
            ttl=self.config.interval * 2,
            version=self.config.version,
            tags=self.config.tags,
        )

    async def _collect_metrics(self) -> SystemMetrics:
        """Collect system metrics."""
        metrics = SystemMetrics()

        if not self.config.include_system_metrics or not self._psutil_available:
            return metrics

        try:
            import psutil

            # CPU usage
            metrics.cpu_percent = psutil.cpu_percent(interval=0.1)

            # Memory usage
            memory = psutil.virtual_memory()
            metrics.memory_percent = memory.percent

            # Disk usage
            disk = psutil.disk_usage("/")
            metrics.disk_usage_percent = (disk.used / disk.total) * 100

            # Uptime
            metrics.uptime_seconds = self.uptime_seconds

        except Exception as e:
            logger.warning(f"Failed to collect system metrics: {e}")

        return metrics

    def on_status_change(self, callback: Callable[[HealthStatus], None]) -> None:
        """Register a callback for status changes."""
        self._status_callbacks.append(callback)

    def remove_callback(self, callback: Callable[[HealthStatus], None]) -> bool:
        """Remove a status change callback."""
        if callback in self._status_callbacks:
            self._status_callbacks.remove(callback)
            return True
        return False

    async def send_status_update(
        self, status: HealthStatus, message: Optional[str] = None
    ) -> bool:
        """
        Send an immediate status update (not waiting for next heartbeat).

        Args:
            status: The new health status
            message: Optional status message

        Returns:
            True if successfully sent
        """
        if not self._http_client:
            return False

        heartbeat = Heartbeat(
            agent_id=self.config.agent_id,
            timestamp=datetime.now(),
            status=status,
            metrics=await self._collect_metrics(),
            message=message,
            ttl=self.config.interval * 2,
            version=self.config.version,
            tags=self.config.tags,
        )

        url = f"{self.config.monitor_url}/api/v1/heartbeat"

        try:
            response = await self._http_client.post(
                url,
                json=heartbeat.to_dict(),
            )
            return response.status_code < 400
        except Exception as e:
            logger.warning(f"Failed to send status update: {e}")
            return False

    async def check_health(self) -> HealthStatus:
        """
        Check the health of the heartbeat service.

        Returns the last known status.
        """
        if not self._http_client:
            return HealthStatus.UNKNOWN

        url = f"{self.config.monitor_url}/api/v1/heartbeat/{self.config.agent_id}"

        try:
            response = await self._http_client.get(url)
            if response.status_code == 200:
                data = response.json()
                status_str = data.get("status", "unknown")
                return HealthStatus(status_str)
        except Exception as e:
            logger.debug(f"Health check failed: {e}")

        return HealthStatus.UNKNOWN

    def get_stats(self) -> dict[str, Any]:
        """Get client statistics."""
        return {
            "agent_id": self.config.agent_id,
            "running": self._running,
            "connected": self._connected,
            "uptime_seconds": self.uptime_seconds,
            "last_heartbeat_time": self._last_heartbeat_time,
            "consecutive_failures": self._consecutive_failures,
            "interval": self.config.interval,
            "monitor_url": self.config.monitor_url,
        }


class HeartbeatClientPool:
    """
    Pool for managing multiple heartbeat clients.

    Useful when running multiple agents in the same process.
    """

    def __init__(self) -> None:
        self._clients: dict[str, AgentHeartbeatClient] = {}

    async def register(
        self,
        agent_id: str,
        monitor_url: str,
        **kwargs: Any,
    ) -> AgentHeartbeatClient:
        """Register a new agent for heartbeat monitoring."""
        if agent_id in self._clients:
            logger.warning(
                f"Agent {agent_id} already registered, stopping existing client"
            )
            await self._clients[agent_id].stop()

        client = AgentHeartbeatClient(agent_id, monitor_url, **kwargs)
        self._clients[agent_id] = client
        await client.start()
        return client

    async def unregister(self, agent_id: str) -> bool:
        """Unregister an agent from heartbeat monitoring."""
        if agent_id in self._clients:
            await self._clients[agent_id].stop()
            del self._clients[agent_id]
            return True
        return False

    def get_client(self, agent_id: str) -> Optional[AgentHeartbeatClient]:
        """Get a heartbeat client by agent ID."""
        return self._clients.get(agent_id)

    async def stop_all(self) -> None:
        """Stop all heartbeat clients."""
        for client in list(self._clients.values()):
            await client.stop()
        self._clients.clear()

    def list_clients(self) -> list[str]:
        """List all registered agent IDs."""
        return list(self._clients.keys())
