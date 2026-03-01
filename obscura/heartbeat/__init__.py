"""
obscura.heartbeat — Heartbeat monitoring system for Obscura agents.

This module provides:
- HeartbeatMonitor: Service that tracks agent health
- AgentHeartbeatClient: Client for agents to send heartbeats
- HealthStatus, Heartbeat: Core types
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

from obscura.heartbeat.types import (
    HealthStatus,
    Heartbeat,
    HealthCheck,
    HealthRecord,
    Alert,
    SystemMetrics,
    HealthStatusTransition,
)
from obscura.heartbeat.store import (
    HeartbeatStore,
    InMemoryHeartbeatStore,
    FileHeartbeatStore,
    get_default_store,
    set_default_store,
)
from obscura.heartbeat.alerts import (
    AlertManager,
    AlertChannel,
    AlertRule,
    LoggingAlertChannel,
    WebhookAlertChannel,
    SlackAlertChannel,
    get_default_alert_manager,
    set_default_alert_manager,
)
from obscura.heartbeat.monitor import (
    HeartbeatMonitor,
    get_default_monitor,
    set_default_monitor,
)
from obscura.heartbeat.client import (
    AgentHeartbeatClient,
    HeartbeatClientConfig,
    HeartbeatClientPool,
)

__all__ = [
    # Types
    "HealthStatus",
    "Heartbeat",
    "HealthCheck",
    "HealthRecord",
    "Alert",
    "SystemMetrics",
    "HealthStatusTransition",
    # Storage
    "HeartbeatStore",
    "InMemoryHeartbeatStore",
    "FileHeartbeatStore",
    "get_default_store",
    "set_default_store",
    # Alerts
    "AlertManager",
    "AlertChannel",
    "AlertRule",
    "LoggingAlertChannel",
    "WebhookAlertChannel",
    "SlackAlertChannel",
    "get_default_alert_manager",
    "set_default_alert_manager",
    # Monitor
    "HeartbeatMonitor",
    "get_default_monitor",
    "set_default_monitor",
    # Client
    "AgentHeartbeatClient",
    "HeartbeatClientConfig",
    "HeartbeatClientPool",
]
