"""Tests for sdk.heartbeat.monitor — HeartbeatMonitor."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta

from sdk.heartbeat.monitor import (
    HeartbeatMonitor,
    get_default_monitor,
    set_default_monitor,
)
from sdk.heartbeat.types import (
    Heartbeat,
    HealthRecord,
    HealthStatus,
    HealthStatusTransition,
)


class TestHeartbeatMonitorInit:
    def test_defaults(self):
        monitor = HeartbeatMonitor()
        assert monitor.is_running is False
        assert monitor._check_interval == 10
        assert monitor._warning_threshold == 1.5
        assert monitor._critical_threshold == 3.0

    def test_custom(self):
        store = MagicMock()
        alert_mgr = MagicMock()
        monitor = HeartbeatMonitor(
            store=store,
            alert_manager=alert_mgr,
            check_interval=5,
            warning_threshold=2.0,
            critical_threshold=5.0,
        )
        assert monitor._check_interval == 5
        assert monitor._warning_threshold == 2.0


class TestHeartbeatMonitorLifecycle:
    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        monitor = HeartbeatMonitor()
        await monitor.start()
        assert monitor.is_running is True

        await monitor.stop()
        assert monitor.is_running is False

    @pytest.mark.asyncio
    async def test_start_already_running(self):
        monitor = HeartbeatMonitor()
        await monitor.start()
        # Should not raise
        await monitor.start()
        await monitor.stop()

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self):
        monitor = HeartbeatMonitor()
        await monitor.stop()  # Should not raise


class TestHeartbeatMonitorRegistration:
    @pytest.mark.asyncio
    async def test_register_agent(self):
        store = AsyncMock()
        monitor = HeartbeatMonitor(store=store)
        await monitor.register_agent("a1", expected_interval=30)
        store.register.assert_awaited_once_with("a1", 30)
        assert "a1" in monitor._transitions

    @pytest.mark.asyncio
    async def test_unregister_agent(self):
        store = AsyncMock()
        store.unregister.return_value = True
        monitor = HeartbeatMonitor(store=store)

        # Register first
        await monitor.register_agent("a1")
        # Then unregister
        result = await monitor.unregister_agent("a1")
        assert result is True
        assert "a1" not in monitor._transitions


class TestHeartbeatMonitorRecordHeartbeat:
    @pytest.mark.asyncio
    async def test_record_heartbeat_auto_register(self):
        store = AsyncMock()
        # First get_record returns None (not registered), second returns record
        record = HealthRecord(agent_id="a1")
        store.get_record.side_effect = [None, record]
        store.register = AsyncMock()

        monitor = HeartbeatMonitor(store=store)
        hb = Heartbeat(
            agent_id="a1",
            timestamp=datetime.now(),
            status=HealthStatus.HEALTHY,
            ttl=30,
        )
        await monitor.record_heartbeat(hb)
        store.save.assert_awaited_once_with(hb)
        store.reset_missed_count.assert_awaited_once_with("a1")

    @pytest.mark.asyncio
    async def test_record_heartbeat_existing(self):
        store = AsyncMock()
        record = HealthRecord(agent_id="a1")
        store.get_record.return_value = record

        monitor = HeartbeatMonitor(store=store)
        hb = Heartbeat(
            agent_id="a1",
            timestamp=datetime.now(),
            status=HealthStatus.HEALTHY,
        )
        await monitor.record_heartbeat(hb)
        store.save.assert_awaited_once()


class TestHeartbeatMonitorHealth:
    @pytest.mark.asyncio
    async def test_get_agent_health_unknown(self):
        store = AsyncMock()
        store.get_record.return_value = None
        monitor = HeartbeatMonitor(store=store)

        status = await monitor.get_agent_health("unknown-agent")
        assert status == HealthStatus.UNKNOWN

    @pytest.mark.asyncio
    async def test_get_agent_health_healthy(self):
        store = AsyncMock()
        hb = Heartbeat(
            agent_id="a1",
            timestamp=datetime.now(),
            status=HealthStatus.HEALTHY,
        )
        record = HealthRecord(agent_id="a1", last_heartbeat=hb, expected_interval=30)
        store.get_record.return_value = record

        monitor = HeartbeatMonitor(store=store)
        status = await monitor.get_agent_health("a1")
        assert status == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_get_agent_health_warning(self):
        store = AsyncMock()
        # Heartbeat 50 seconds ago (1.5x * 30 = 45s threshold)
        old_time = datetime.now() - timedelta(seconds=50)
        hb = Heartbeat(
            agent_id="a1",
            timestamp=old_time,
            status=HealthStatus.HEALTHY,
        )
        record = HealthRecord(agent_id="a1", last_heartbeat=hb, expected_interval=30)
        store.get_record.return_value = record

        monitor = HeartbeatMonitor(store=store)
        status = await monitor.get_agent_health("a1")
        assert status == HealthStatus.WARNING

    @pytest.mark.asyncio
    async def test_get_agent_health_critical(self):
        store = AsyncMock()
        # Heartbeat 100 seconds ago (3x * 30 = 90s threshold)
        old_time = datetime.now() - timedelta(seconds=100)
        hb = Heartbeat(
            agent_id="a1",
            timestamp=old_time,
            status=HealthStatus.HEALTHY,
        )
        record = HealthRecord(agent_id="a1", last_heartbeat=hb, expected_interval=30)
        store.get_record.return_value = record

        monitor = HeartbeatMonitor(store=store)
        status = await monitor.get_agent_health("a1")
        assert status == HealthStatus.CRITICAL

    @pytest.mark.asyncio
    async def test_compute_health_no_heartbeat(self):
        store = AsyncMock()
        monitor = HeartbeatMonitor(store=store)
        record = HealthRecord(agent_id="a1")  # No last_heartbeat
        status = await monitor._compute_health("a1", record)
        assert status == HealthStatus.UNKNOWN


class TestHeartbeatMonitorQueries:
    @pytest.mark.asyncio
    async def test_list_agents(self):
        store = AsyncMock()
        store.list_agents.return_value = ["a1", "a2"]
        monitor = HeartbeatMonitor(store=store)
        agents = await monitor.list_agents()
        assert agents == ["a1", "a2"]

    @pytest.mark.asyncio
    async def test_list_records(self):
        store = AsyncMock()
        store.list_records.return_value = [HealthRecord(agent_id="a1")]
        monitor = HeartbeatMonitor(store=store)
        records = await monitor.list_records()
        assert len(records) == 1

    @pytest.mark.asyncio
    async def test_get_health_summary(self):
        store = AsyncMock()
        hb = Heartbeat(
            agent_id="a1", timestamp=datetime.now(), status=HealthStatus.HEALTHY
        )
        store.list_records.return_value = [
            HealthRecord(
                agent_id="a1", computed_status=HealthStatus.HEALTHY, last_heartbeat=hb
            ),
        ]
        monitor = HeartbeatMonitor(store=store)
        summary = await monitor.get_health_summary()
        assert summary["total"] == 1
        assert summary["healthy"] == 1

    @pytest.mark.asyncio
    async def test_get_agent_record(self):
        store = AsyncMock()
        record = HealthRecord(agent_id="a1")
        store.get_record.return_value = record
        monitor = HeartbeatMonitor(store=store)
        result = await monitor.get_agent_record("a1")
        assert result.agent_id == "a1"


class TestHeartbeatMonitorCallbacks:
    def test_on_status_change(self):
        monitor = HeartbeatMonitor()
        cb = MagicMock()
        monitor.on_status_change(cb)
        assert cb in monitor._callbacks

    def test_remove_callback(self):
        monitor = HeartbeatMonitor()
        cb = MagicMock()
        monitor.on_status_change(cb)
        assert monitor.remove_callback(cb) is True
        assert monitor.remove_callback(cb) is False

    def test_get_transitions(self):
        monitor = HeartbeatMonitor()
        assert monitor.get_transitions("a1") is None
        monitor._transitions["a1"] = HealthStatusTransition("a1")
        assert monitor.get_transitions("a1") is not None


class TestHeartbeatMonitorAlertMessage:
    def test_critical_message(self):
        monitor = HeartbeatMonitor()
        record = HealthRecord(agent_id="a1", missed_count=5)
        msg = monitor._generate_alert_message(
            "a1", HealthStatus.WARNING, HealthStatus.CRITICAL, record
        )
        assert "CRITICAL" in msg
        assert "5" in msg

    def test_warning_message(self):
        monitor = HeartbeatMonitor()
        record = HealthRecord(agent_id="a1")
        msg = monitor._generate_alert_message(
            "a1", HealthStatus.HEALTHY, HealthStatus.WARNING, record
        )
        assert "WARNING" in msg

    def test_recovery_message(self):
        monitor = HeartbeatMonitor()
        record = HealthRecord(agent_id="a1")
        msg = monitor._generate_alert_message(
            "a1", HealthStatus.WARNING, HealthStatus.HEALTHY, record
        )
        assert "recovered" in msg


class TestGlobalMonitor:
    def test_get_default_monitor(self):
        monitor = get_default_monitor()
        assert isinstance(monitor, HeartbeatMonitor)

    def test_set_default_monitor(self):
        custom = HeartbeatMonitor(check_interval=99)
        set_default_monitor(custom)
        assert get_default_monitor() is custom
        # Reset
        set_default_monitor(None)
