"""Tests for sdk.heartbeat.alerts — AlertManager, channels, and rules."""

import pytest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from obscura.heartbeat.alerts import (
    AlertManager,
    AlertRule,
    LoggingAlertChannel,
    WebhookAlertChannel,
    get_default_alert_manager,
    set_default_alert_manager,
)
from obscura.heartbeat.types import Alert, HealthRecord, HealthStatus


class TestLoggingAlertChannel:
    @pytest.mark.asyncio
    async def test_send(self) -> None:
        channel = LoggingAlertChannel()
        alert = Alert(
            alert_id="a1",
            agent_id="agent-1",
            severity=HealthStatus.WARNING,
            status=HealthStatus.WARNING,
            message="Test alert",
            timestamp=datetime.now(),
        )
        result = await channel.send(alert)
        assert result is True

    @pytest.mark.asyncio
    async def test_test(self) -> None:
        channel = LoggingAlertChannel()
        result = await channel.test()
        assert result is True


class TestWebhookAlertChannel:
    @pytest.mark.asyncio
    async def test_send_success(self) -> None:
        channel = WebhookAlertChannel("https://example.com/hook")
        alert = Alert(
            alert_id="a1",
            agent_id="agent-1",
            severity=HealthStatus.WARNING,
            status=HealthStatus.WARNING,
            message="Test",
            timestamp=datetime.now(),
        )

        mock_response: Any = MagicMock()
        mock_response.status_code = 200
        mock_client: Any = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await channel.send(alert)
            assert result is True

    @pytest.mark.asyncio
    async def test_send_with_secret(self) -> None:
        channel = WebhookAlertChannel("https://example.com/hook", secret="my-secret")
        alert = Alert(
            alert_id="a1",
            agent_id="agent-1",
            severity=HealthStatus.CRITICAL,
            status=HealthStatus.CRITICAL,
            message="Critical",
            timestamp=datetime.now(),
        )

        mock_response: Any = MagicMock()
        mock_response.status_code = 200
        mock_client: Any = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await channel.send(alert)
            assert result is True
            # Verify signature header was set
            call_args = mock_client.post.call_args
            headers: dict[str, str] = call_args.kwargs.get("headers", {})
            assert "X-Webhook-Signature" in headers

    @pytest.mark.asyncio
    async def test_send_failure(self) -> None:
        channel = WebhookAlertChannel("https://example.com/hook", retries=1)
        alert = Alert(
            alert_id="a1",
            agent_id="agent-1",
            severity=HealthStatus.WARNING,
            status=HealthStatus.WARNING,
            message="Test",
            timestamp=datetime.now(),
        )

        mock_client: Any = AsyncMock()
        mock_client.post.side_effect = ConnectionError("refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await channel.send(alert)
            assert result is False

    @pytest.mark.asyncio
    async def test_test_success(self) -> None:
        channel = WebhookAlertChannel("https://example.com/hook")
        mock_response: Any = MagicMock()
        mock_response.status_code = 200
        mock_client: Any = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await channel.test()
            assert result is True


def _missed_count_condition(r: HealthRecord) -> bool:
    return r.missed_count > 0


def _always_true_condition(r: HealthRecord) -> bool:
    return True


def _critical_condition(r: HealthRecord) -> bool:
    return r.computed_status == HealthStatus.CRITICAL


class TestAlertRule:
    def test_should_alert_warning(self) -> None:
        rule = AlertRule(
            name="test-rule",
            condition=_missed_count_condition,
            severity=HealthStatus.WARNING,
        )
        record = HealthRecord(
            agent_id="a1",
            computed_status=HealthStatus.WARNING,
            missed_count=2,
        )
        assert rule.should_alert(record) is True

    def test_should_not_alert_healthy(self) -> None:
        rule = AlertRule(
            name="test-rule",
            condition=_always_true_condition,
        )
        record = HealthRecord(
            agent_id="a1",
            computed_status=HealthStatus.HEALTHY,
        )
        assert rule.should_alert(record) is False


class TestAlertManager:
    def test_init_has_default_channel(self) -> None:
        mgr = AlertManager()
        assert mgr.get_channel("default") is not None

    def test_add_remove_channel(self) -> None:
        mgr = AlertManager()
        channel = LoggingAlertChannel()
        mgr.add_channel("test", channel)
        assert mgr.get_channel("test") is channel
        assert mgr.remove_channel("test") is True
        assert mgr.remove_channel("test") is False

    def test_add_remove_rule(self) -> None:
        mgr = AlertManager()
        rule = AlertRule(name="r1", condition=_always_true_condition)
        mgr.add_rule(rule)
        assert mgr.remove_rule("r1") is True
        assert mgr.remove_rule("r1") is False

    def test_create_alert(self) -> None:
        mgr = AlertManager()
        alert = mgr.create_alert(
            "a1", HealthStatus.WARNING, HealthStatus.WARNING, "test msg"
        )
        assert alert.agent_id == "a1"
        assert alert.message == "test msg"
        assert len(mgr._alerts) == 1  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_trigger_default_channel(self) -> None:
        mgr = AlertManager()
        record = HealthRecord(
            agent_id="a1",
            computed_status=HealthStatus.WARNING,
        )
        alerts = await mgr.trigger(record, "Agent down")
        assert len(alerts) == 1
        assert alerts[0].message == "Agent down"

    @pytest.mark.asyncio
    async def test_trigger_with_rule(self) -> None:
        mgr = AlertManager()
        rule = AlertRule(
            name="test-rule",
            condition=_critical_condition,
            channels=["default"],
        )
        mgr.add_rule(rule)

        record = HealthRecord(
            agent_id="a1",
            computed_status=HealthStatus.CRITICAL,
            missed_count=5,
        )
        alerts = await mgr.trigger(record)
        assert len(alerts) >= 1

    @pytest.mark.asyncio
    async def test_trigger_healthy_no_alert(self) -> None:
        mgr = AlertManager()
        record = HealthRecord(
            agent_id="a1",
            computed_status=HealthStatus.HEALTHY,
        )
        alerts = await mgr.trigger(record)
        assert len(alerts) == 0

    @pytest.mark.asyncio
    async def test_acknowledge_alert(self) -> None:
        mgr = AlertManager()
        alert = mgr.create_alert(
            "a1", HealthStatus.WARNING, HealthStatus.WARNING, "test"
        )
        ack = await mgr.acknowledge_alert(alert.alert_id, "admin")
        assert ack is not None
        assert ack.acknowledged is True
        assert ack.acknowledged_by == "admin"

    @pytest.mark.asyncio
    async def test_acknowledge_alert_not_found(self) -> None:
        mgr = AlertManager()
        result = await mgr.acknowledge_alert("nonexistent", "admin")
        assert result is None

    def test_get_alerts_filtered(self) -> None:
        mgr = AlertManager()
        mgr.create_alert("a1", HealthStatus.WARNING, HealthStatus.WARNING, "m1")
        mgr.create_alert("a2", HealthStatus.CRITICAL, HealthStatus.CRITICAL, "m2")

        all_alerts = mgr.get_alerts()
        assert len(all_alerts) == 2

        a1_alerts = mgr.get_alerts(agent_id="a1")
        assert len(a1_alerts) == 1

        unack = mgr.get_alerts(acknowledged=False)
        assert len(unack) == 2

    @pytest.mark.asyncio
    async def test_test_channel(self) -> None:
        mgr = AlertManager()
        result = await mgr.test_channel("default")
        assert result is True

        result = await mgr.test_channel("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_test_all_channels(self) -> None:
        mgr = AlertManager()
        results = await mgr.test_all_channels()
        assert "default" in results
        assert results["default"] is True


class TestGlobalAlertManager:
    def test_get_default_alert_manager(self) -> None:
        mgr = get_default_alert_manager()
        assert isinstance(mgr, AlertManager)

    def test_set_default_alert_manager(self) -> None:
        custom = AlertManager()
        set_default_alert_manager(custom)
        assert get_default_alert_manager() is custom
        set_default_alert_manager(None)
