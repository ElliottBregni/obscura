"""obscura.heartbeat — Heartbeat monitoring system for Obscura agents.

This module provides:
- HeartbeatMonitor: Service that tracks agent health
- AgentHeartbeatClient: Client for agents to send heartbeats
- AgentHealthStatus, Heartbeat: Core types
- AlertManager: Alert management with webhook support
- HeartbeatStore: Storage backends for heartbeat data

Usage:
    # Monitor side
    from obscura.heartbeat import HeartbeatMonitor

    monitor = HeartbeatMonitor()
    await monitor.start()
    await monitor.register_agent("agent-123")

    # Agent side
    from obscura.heartbeat import AgentHeartbeatClient

    client = AgentHeartbeatClient("agent-123", "http://localhost:8080")
    await client.start()
"""

from __future__ import annotations

from obscura.heartbeat.alerts import (
    AlertChannel,
    AlertManager,
    AlertRule,
    LoggingAlertChannel,
    SlackAlertChannel,
    WebhookAlertChannel,
    get_default_alert_manager,
    set_default_alert_manager,
)
from obscura.heartbeat.client import (
    AgentHeartbeatClient,
    HeartbeatClientConfig,
    HeartbeatClientPool,
)
from obscura.heartbeat.monitor import (
    HeartbeatMonitor,
    get_default_monitor,
    set_default_monitor,
)
from obscura.heartbeat.store import (
    FileHeartbeatStore,
    HeartbeatStore,
    InMemoryHeartbeatStore,
    get_default_store,
    set_default_store,
)
from obscura.heartbeat.types import (
    Alert,
    HealthCheck,
    HealthRecord,
    HealthStatusTransition,
    Heartbeat,
    SystemMetrics,
)

__all__ = [
    # Client
    "AgentHeartbeatClient",
    "Alert",
    "AlertChannel",
    # Alerts
    "AlertManager",
    "AlertRule",
    "FileHeartbeatStore",
    "HealthCheck",
    "HealthRecord",
    # Types
    "HealthStatusTransition",
    "Heartbeat",
    "HeartbeatClientConfig",
    "HeartbeatClientPool",
    # Monitor
    "HeartbeatMonitor",
    # Storage
    "HeartbeatStore",
    "InMemoryHeartbeatStore",
    "LoggingAlertChannel",
    "SlackAlertChannel",
    "SystemMetrics",
    "WebhookAlertChannel",
    "get_default_alert_manager",
    "get_default_monitor",
    "get_default_store",
    "set_default_alert_manager",
    "set_default_monitor",
    "set_default_store",
]
