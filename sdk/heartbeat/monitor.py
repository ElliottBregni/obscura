"""
sdk.heartbeat.monitor — Heartbeat monitoring service.

Monitors agent health via heartbeats, detects missing heartbeats,
and triggers alerts on health changes.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime
from typing import Any, Optional, Callable

from sdk.heartbeat.types import (
    Heartbeat,
    HealthRecord,
    HealthStatus,
    HealthStatusTransition,
)
from sdk.heartbeat.store import HeartbeatStore, get_default_store
from sdk.heartbeat.alerts import AlertManager, get_default_alert_manager

logger = logging.getLogger(__name__)


class HeartbeatMonitor:
    """
    Monitors agent health via heartbeats.

    This service:
    - Registers agents for monitoring
    - Receives and stores heartbeats
    - Detects missing heartbeats (WARNING/CRITICAL)
    - Triggers alerts on health changes

    Usage:
        monitor = HeartbeatMonitor()
        await monitor.start()

        # Register an agent
        await monitor.register_agent("agent-123", expected_interval=30)

        # Record a heartbeat
        heartbeat = Heartbeat(agent_id="agent-123", ...)
        await monitor.record_heartbeat(heartbeat)

        # Get health status
        status = await monitor.get_agent_health("agent-123")
    """

    def __init__(
        self,
        store: Optional[HeartbeatStore] = None,
        alert_manager: Optional[AlertManager] = None,
        check_interval: int = 10,
        warning_threshold: float = 1.5,  # 1.5x expected interval
        critical_threshold: float = 3.0,  # 3x expected interval
    ) -> None:
        """
        Initialize the heartbeat monitor.

        Args:
            store: Storage backend for heartbeats (defaults to in-memory)
            alert_manager: Alert manager for health alerts
            check_interval: How often to check agent health (seconds)
            warning_threshold: Multiplier for WARNING status
            critical_threshold: Multiplier for CRITICAL status
        """
        self._store = store or get_default_store()
        self._alert_manager = alert_manager or get_default_alert_manager()
        self._check_interval = check_interval
        self._warning_threshold = warning_threshold
        self._critical_threshold = critical_threshold

        self._running = False
        self._monitor_task: Optional[asyncio.Task[None]] = None
        self._transitions: dict[str, HealthStatusTransition] = {}
        self._callbacks: list[Callable[[str, HealthStatus, HealthStatus], None]] = []

        # Track last check time for each agent
        self._last_check: dict[str, datetime] = {}

    async def start(self) -> None:
        """Start the monitoring loop."""
        if self._running:
            logger.warning("HeartbeatMonitor already running")
            return

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("HeartbeatMonitor started")

    async def stop(self) -> None:
        """Stop the monitoring loop."""
        if not self._running:
            return

        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        logger.info("HeartbeatMonitor stopped")

    @property
    def is_running(self) -> bool:
        """Check if the monitor is running."""
        return self._running

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                await self._check_all_agents()
            except Exception as e:
                logger.exception(f"Error in monitor loop: {e}")

            try:
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                break

    async def _check_all_agents(self) -> None:
        """Check all registered agents for health status."""
        agents = await self._store.list_agents()
        now = datetime.now()

        for agent_id in agents:
            try:
                record = await self._store.get_record(agent_id)
                if not record:
                    continue

                # Skip if already checked recently
                if agent_id in self._last_check:
                    if (
                        now - self._last_check[agent_id]
                    ).total_seconds() < self._check_interval:
                        continue

                self._last_check[agent_id] = now

                # Compute current health based on heartbeat timing
                new_status = await self._compute_health(agent_id, record)

                # Check for status transition
                old_status = record.computed_status
                if old_status != new_status:
                    await self._handle_status_change(agent_id, old_status, new_status)

                # Update stored status
                await self._store.update_computed_status(agent_id, new_status)

            except Exception as e:
                logger.exception(f"Error checking agent {agent_id}: {e}")

    async def _compute_health(
        self, agent_id: str, record: HealthRecord
    ) -> HealthStatus:
        """
        Compute the health status for an agent based on heartbeat timing.

        Status determination:
        - UNKNOWN: No heartbeat received yet
        - HEALTHY: Heartbeat received within expected interval
        - WARNING: No heartbeat for warning_threshold * expected_interval
        - CRITICAL: No heartbeat for critical_threshold * expected_interval
        """
        last_heartbeat = record.last_heartbeat

        if not last_heartbeat:
            return HealthStatus.UNKNOWN

        now = datetime.now()
        elapsed = (now - last_heartbeat.timestamp).total_seconds()
        expected_interval = record.expected_interval or 30

        # Calculate thresholds
        warning_time = expected_interval * self._warning_threshold
        critical_time = expected_interval * self._critical_threshold

        if elapsed >= critical_time:
            return HealthStatus.CRITICAL
        elif elapsed >= warning_time:
            return HealthStatus.WARNING
        else:
            # Use reported status if heartbeat is timely
            return last_heartbeat.status

    async def _handle_status_change(
        self,
        agent_id: str,
        old_status: HealthStatus,
        new_status: HealthStatus,
    ) -> None:
        """Handle a health status change."""
        logger.info(
            f"Agent {agent_id} status changed: {old_status.value} -> {new_status.value}"
        )

        # Track transition
        if agent_id not in self._transitions:
            self._transitions[agent_id] = HealthStatusTransition(agent_id)

        transition = self._transitions[agent_id]
        transition.record_transition(old_status, new_status)

        # Get the health record for alerting
        record = await self._store.get_record(agent_id)
        if record:
            # Generate alert message
            message = self._generate_alert_message(
                agent_id, old_status, new_status, record
            )

            # Trigger alert
            await self._alert_manager.trigger(record, message)

        # Notify callbacks
        for callback in self._callbacks:
            try:
                if inspect.iscoroutinefunction(callback):
                    await callback(agent_id, old_status, new_status)
                else:
                    callback(agent_id, old_status, new_status)
            except Exception as e:
                logger.warning(f"Callback error for agent {agent_id}: {e}")

    def _generate_alert_message(
        self,
        agent_id: str,
        old_status: HealthStatus,
        new_status: HealthStatus,
        record: HealthRecord,
    ) -> str:
        """Generate an alert message for a status change."""
        if new_status == HealthStatus.CRITICAL:
            missed = record.missed_count
            return f"Agent {agent_id} is CRITICAL - {missed} heartbeats missed"
        elif new_status == HealthStatus.WARNING:
            return f"Agent {agent_id} is WARNING - heartbeats delayed"
        elif new_status == HealthStatus.HEALTHY:
            return f"Agent {agent_id} recovered to HEALTHY"
        else:
            return f"Agent {agent_id} status: {new_status.value}"

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def register_agent(self, agent_id: str, expected_interval: int = 30) -> None:
        """
        Register an agent for monitoring.

        Args:
            agent_id: Unique identifier for the agent
            expected_interval: Expected seconds between heartbeats
        """
        await self._store.register(agent_id, expected_interval)
        self._transitions[agent_id] = HealthStatusTransition(agent_id)
        logger.debug(
            f"Registered agent {agent_id} for monitoring (interval: {expected_interval}s)"
        )

    async def unregister_agent(self, agent_id: str) -> bool:
        """Unregister an agent from monitoring."""
        result = await self._store.unregister(agent_id)
        if agent_id in self._transitions:
            del self._transitions[agent_id]
        if agent_id in self._last_check:
            del self._last_check[agent_id]
        logger.debug(f"Unregistered agent {agent_id}")
        return result

    async def record_heartbeat(self, heartbeat: Heartbeat) -> None:
        """
        Record a heartbeat from an agent.

        Args:
            heartbeat: The heartbeat to record
        """
        agent_id = heartbeat.agent_id

        # Auto-register if not already registered
        record = await self._store.get_record(agent_id)
        if not record:
            await self.register_agent(agent_id, heartbeat.ttl or 30)
            record = await self._store.get_record(agent_id)

        # Reset missed count on successful heartbeat
        await self._store.reset_missed_count(agent_id)

        # Save the heartbeat
        await self._store.save(heartbeat)

        logger.debug(f"Recorded heartbeat from agent {agent_id}")

    async def get_agent_health(self, agent_id: str) -> HealthStatus:
        """
        Get the current health status of an agent.

        Returns UNKNOWN if agent is not registered.
        """
        record = await self._store.get_record(agent_id)
        if not record:
            return HealthStatus.UNKNOWN

        return await self._compute_health(agent_id, record)

    async def get_agent_record(self, agent_id: str) -> Optional[HealthRecord]:
        """Get the full health record for an agent."""
        return await self._store.get_record(agent_id)

    async def list_agents(self) -> list[str]:
        """List all registered agent IDs."""
        return await self._store.list_agents()

    async def list_records(self) -> list[HealthRecord]:
        """List all health records."""
        return await self._store.list_records()

    async def get_health_summary(self) -> dict[str, Any]:
        """Get a summary of all agent health statuses."""
        records = await self._store.list_records()

        counts: dict[str, int] = {
            "total": len(records),
            "healthy": 0,
            "warning": 0,
            "critical": 0,
            "unknown": 0,
        }
        agents_list: list[dict[str, Any]] = []

        for record in records:
            status = record.computed_status.value
            if status in counts:
                counts[status] += 1

            agents_list.append(
                {
                    "agent_id": record.agent_id,
                    "status": status,
                    "last_heartbeat": record.last_heartbeat.timestamp.isoformat()
                    if record.last_heartbeat
                    else None,
                    "missed_count": record.missed_count,
                }
            )

        summary: dict[str, Any] = {**counts, "agents": agents_list}
        return summary

    def on_status_change(
        self,
        callback: Callable[[str, HealthStatus, HealthStatus], None],
    ) -> None:
        """
        Register a callback for status changes.

        Callback signature: (agent_id, old_status, new_status)
        """
        self._callbacks.append(callback)

    def remove_callback(
        self,
        callback: Callable[[str, HealthStatus, HealthStatus], None],
    ) -> bool:
        """Remove a status change callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)
            return True
        return False

    def get_transitions(self, agent_id: str) -> Optional[HealthStatusTransition]:
        """Get the status transition history for an agent."""
        return self._transitions.get(agent_id)


# Global monitor instance
_default_monitor: Optional[HeartbeatMonitor] = None


def get_default_monitor() -> HeartbeatMonitor:
    """Get or create the default heartbeat monitor."""
    global _default_monitor
    if _default_monitor is None:
        _default_monitor = HeartbeatMonitor()
    return _default_monitor


def set_default_monitor(monitor: HeartbeatMonitor) -> None:
    """Set the default heartbeat monitor."""
    global _default_monitor
    _default_monitor = monitor
