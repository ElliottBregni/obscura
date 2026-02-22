"""
obscura.heartbeat.alerts — Alert management system.

Provides alert channels (webhook, logging, etc.) and alert routing.
"""

from __future__ import annotations

import json
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, override

import httpx

from obscura.heartbeat.types import Alert, HealthRecord, HealthStatus

logger = logging.getLogger(__name__)


@dataclass
class AlertRule:
    """
    Rule for triggering alerts based on health status.

    Attributes:
        name: Rule identifier
        condition: Function that evaluates if alert should fire
        severity: Minimum severity level to trigger
        cooldown: Minimum seconds between repeated alerts for same agent
        channels: Channel names to send alerts to
    """

    name: str
    condition: Callable[..., Any] = field(compare=False)
    severity: HealthStatus = HealthStatus.WARNING
    cooldown: int = 300  # 5 minutes
    channels: list[str] = field(default_factory=lambda: ["default"])

    def should_alert(self, record: HealthRecord) -> bool:
        """Check if this rule should trigger an alert."""
        if record.computed_status.value not in ("warning", "critical"):
            return False
        return self.condition(record)


class AlertChannel(ABC):
    """Abstract base class for alert channels."""

    name: str = "unknown"

    @abstractmethod
    async def send(self, alert: Alert) -> bool:
        """Send an alert through this channel. Returns success status."""
        pass

    @abstractmethod
    async def test(self) -> bool:
        """Test the channel configuration."""
        pass


class LoggingAlertChannel(AlertChannel):
    """Alert channel that logs alerts."""

    name = "logging"

    def __init__(self, log_level: int = logging.WARNING) -> None:
        self.log_level = log_level

    @override
    async def send(self, alert: Alert) -> bool:
        """Log the alert."""
        message = f"[ALERT] Agent {alert.agent_id}: {alert.message} (status: {alert.status.value})"
        logger.log(self.log_level, message)
        return True

    @override
    async def test(self) -> bool:
        """Test the channel."""
        return True


class WebhookAlertChannel(AlertChannel):
    """
    Alert channel that sends alerts via HTTP webhook.

    Supports POST requests with JSON payloads.
    """

    name = "webhook"

    def __init__(
        self,
        webhook_url: str,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 30.0,
        retries: int = 3,
        secret: Optional[str] = None,  # For webhook signature
    ) -> None:
        self.webhook_url = webhook_url
        self.headers = headers or {"Content-Type": "application/json"}
        self.timeout = timeout
        self.retries = retries
        self.secret = secret

    @override
    async def send(self, alert: Alert) -> bool:
        """Send alert via webhook POST request."""
        payload: dict[str, str | bool] = {
            "alert_id": alert.alert_id,
            "agent_id": alert.agent_id,
            "severity": alert.severity.value,
            "status": alert.status.value,
            "message": alert.message,
            "timestamp": alert.timestamp.isoformat(),
            "acknowledged": alert.acknowledged,
        }

        headers = dict(self.headers)
        if self.secret:
            import hmac
            import hashlib

            payload_str = json.dumps(payload, sort_keys=True)
            signature = hmac.new(
                self.secret.encode(), payload_str.encode(), hashlib.sha256
            ).hexdigest()
            headers["X-Webhook-Signature"] = f"sha256={signature}"

        for attempt in range(self.retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        self.webhook_url,
                        json=payload,
                        headers=headers,
                    )
                    if response.status_code < 400:
                        logger.debug(f"Alert sent to webhook: {alert.alert_id}")
                        return True
                    else:
                        logger.warning(
                            f"Webhook returned {response.status_code}: {response.text}"
                        )
            except httpx.TimeoutException:
                logger.warning(
                    f"Webhook timeout (attempt {attempt + 1}/{self.retries})"
                )
            except Exception as e:
                logger.warning(
                    f"Webhook error (attempt {attempt + 1}/{self.retries}): {e}"
                )

        logger.error(f"Failed to send alert to webhook after {self.retries} attempts")
        return False

    @override
    async def test(self) -> bool:
        """Test the webhook with a ping message."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    self.webhook_url,
                    json={"type": "test", "message": "Heartbeat alert channel test"},
                    headers=self.headers,
                )
                return response.status_code < 400
        except Exception as e:
            logger.warning(f"Webhook test failed: {e}")
            return False


class SlackAlertChannel(AlertChannel):
    """Alert channel that sends alerts to Slack."""

    name = "slack"

    def __init__(
        self,
        webhook_url: str,
        channel: Optional[str] = None,
        username: str = "Obscura Heartbeat",
        emoji: str = ":heartpulse:",
    ) -> None:
        self.webhook_url = webhook_url
        self.channel = channel
        self.username = username
        self.emoji = emoji
        self._http = WebhookAlertChannel(webhook_url)

    @override
    async def send(self, alert: Alert) -> bool:
        """Send alert to Slack webhook."""
        # Format message based on severity
        color = {
            HealthStatus.WARNING: "warning",  # yellow
            HealthStatus.CRITICAL: "danger",  # red
        }.get(alert.severity, "#808080")

        attachment: dict[str, Any] = {
            "color": color,
            "title": f"Health Alert: {alert.agent_id}",
            "text": alert.message,
            "fields": [
                {"title": "Status", "value": alert.status.value, "short": True},
                {"title": "Severity", "value": alert.severity.value, "short": True},
                {
                    "title": "Time",
                    "value": alert.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    "short": True,
                },
            ],
            "footer": "Obscura Heartbeat",
            "ts": int(alert.timestamp.timestamp()),
        }

        payload: dict[str, Any] = {
            "username": self.username,
            "icon_emoji": self.emoji,
            "attachments": [attachment],
        }

        if self.channel:
            payload["channel"] = self.channel

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.webhook_url,
                    json=payload,
                )
                return response.status_code == 200
        except Exception as e:
            logger.warning(f"Slack alert failed: {e}")
            return False

    @override
    async def test(self) -> bool:
        """Test the Slack webhook."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                payload = {
                    "username": self.username,
                    "icon_emoji": self.emoji,
                    "text": "Obscura Heartbeat alert channel test",
                }
                if self.channel:
                    payload["channel"] = self.channel
                response = await client.post(
                    self.webhook_url,
                    json=payload,
                )
                return response.status_code == 200
        except Exception as e:
            logger.warning(f"Slack webhook test failed: {e}")
            return False


class NativeNotificationChannel(AlertChannel):
    """Alert channel that fires macOS native notifications.

    Maps :class:`HealthStatus` severity to :class:`AttentionPriority`
    so that critical alerts become modal popups and warnings become
    banner notifications.
    """

    name = "native"

    def __init__(self) -> None:
        from obscura.notifications.native import NativeNotifier

        self._notifier = NativeNotifier()

    @override
    async def send(self, alert: Alert) -> bool:
        """Send a native macOS notification for the alert."""
        from obscura.agent.interaction import AttentionPriority

        priority_map: dict[HealthStatus, AttentionPriority] = {
            HealthStatus.CRITICAL: AttentionPriority.CRITICAL,
            HealthStatus.WARNING: AttentionPriority.HIGH,
            HealthStatus.HEALTHY: AttentionPriority.NORMAL,
        }
        priority = priority_map.get(alert.severity, AttentionPriority.NORMAL)

        try:
            await self._notifier.attention(
                title=f"Agent Alert: {alert.agent_id}",
                message=alert.message,
                priority=priority,
            )
            return True
        except Exception as e:
            logger.warning("Native notification failed: %s", e)
            return False

    @override
    async def test(self) -> bool:
        """Test the native notification channel."""
        from obscura.agent.interaction import AttentionPriority

        try:
            await self._notifier.notify(
                title="Obscura Heartbeat",
                message="Alert channel test notification",
                priority=AttentionPriority.NORMAL,
            )
            return True
        except Exception:
            return False


class AlertManager:
    """
    Manages health alerts and routing to channels.

    Supports multiple alert channels and rules-based routing.
    """

    def __init__(self) -> None:
        self._channels: dict[str, AlertChannel] = {}
        self._rules: list[AlertRule] = []
        self._alerts: list[Alert] = []
        self._last_alert_time: dict[str, datetime] = {}  # agent_id -> last alert time

        # Add default logging channel
        self.add_channel("default", LoggingAlertChannel())

    def add_channel(self, name: str, channel: AlertChannel) -> None:
        """Add an alert channel."""
        self._channels[name] = channel
        logger.debug(f"Added alert channel: {name} ({channel.name})")

    def remove_channel(self, name: str) -> bool:
        """Remove an alert channel."""
        if name in self._channels:
            del self._channels[name]
            return True
        return False

    def get_channel(self, name: str) -> Optional[AlertChannel]:
        """Get an alert channel by name."""
        return self._channels.get(name)

    def add_rule(self, rule: AlertRule) -> None:
        """Add an alert rule."""
        self._rules.append(rule)
        logger.debug(f"Added alert rule: {rule.name}")

    def remove_rule(self, name: str) -> bool:
        """Remove an alert rule by name."""
        for i, rule in enumerate(self._rules):
            if rule.name == name:
                self._rules.pop(i)
                return True
        return False

    def create_alert(
        self,
        agent_id: str,
        severity: HealthStatus,
        status: HealthStatus,
        message: str,
    ) -> Alert:
        """Create a new alert."""
        alert = Alert(
            alert_id=str(uuid.uuid4()),
            agent_id=agent_id,
            severity=severity,
            status=status,
            message=message,
            timestamp=datetime.now(),
        )
        self._alerts.append(alert)
        return alert

    async def trigger(
        self,
        record: HealthRecord,
        message: Optional[str] = None,
    ) -> list[Alert]:
        """
        Trigger alerts for a health record.

        Evaluates rules and sends alerts through appropriate channels.
        """
        triggered: list[Alert] = []
        agent_id = record.agent_id

        # Check cooldown
        now = datetime.now()
        if agent_id in self._last_alert_time:
            last_time = self._last_alert_time[agent_id]
            for rule in self._rules:
                if (now - last_time).total_seconds() < rule.cooldown:
                    logger.debug(f"Alert cooldown active for {agent_id}")
                    continue

        # Evaluate rules
        for rule in self._rules:
            if rule.should_alert(record):
                alert_message = (
                    message or f"Agent {agent_id} is {record.computed_status.value}"
                )
                alert = self.create_alert(
                    agent_id=agent_id,
                    severity=rule.severity,
                    status=record.computed_status,
                    message=alert_message,
                )

                # Send to configured channels
                for channel_name in rule.channels:
                    channel = self._channels.get(channel_name)
                    if channel:
                        success = await channel.send(alert)
                        if success:
                            triggered.append(alert)
                            self._last_alert_time[agent_id] = now
                            record.alert_count += 1
                    else:
                        logger.warning(f"Alert channel not found: {channel_name}")

        # Always send to default channel if no rules matched but status is bad
        if not triggered and record.computed_status in (
            HealthStatus.WARNING,
            HealthStatus.CRITICAL,
        ):
            alert_message = (
                message or f"Agent {agent_id} is {record.computed_status.value}"
            )
            alert = self.create_alert(
                agent_id=agent_id,
                severity=record.computed_status,
                status=record.computed_status,
                message=alert_message,
            )
            default_channel = self._channels.get("default")
            if default_channel:
                success = await default_channel.send(alert)
                if success:
                    triggered.append(alert)
                    self._last_alert_time[agent_id] = now
                    record.alert_count += 1

        return triggered

    async def acknowledge_alert(
        self,
        alert_id: str,
        acknowledged_by: str,
    ) -> Optional[Alert]:
        """Acknowledge an alert."""
        for alert in self._alerts:
            if alert.alert_id == alert_id:
                alert.acknowledged = True
                alert.acknowledged_by = acknowledged_by
                alert.acknowledged_at = datetime.now()
                return alert
        return None

    def get_alerts(
        self,
        agent_id: Optional[str] = None,
        acknowledged: Optional[bool] = None,
        limit: int = 100,
    ) -> list[Alert]:
        """Get alerts with optional filtering."""
        alerts = self._alerts

        if agent_id:
            alerts = [a for a in alerts if a.agent_id == agent_id]

        if acknowledged is not None:
            alerts = [a for a in alerts if a.acknowledged == acknowledged]

        # Sort by timestamp (newest first) and limit
        alerts = sorted(alerts, key=lambda a: a.timestamp, reverse=True)[:limit]
        return alerts

    async def test_channel(self, name: str) -> bool:
        """Test an alert channel."""
        channel = self._channels.get(name)
        if channel:
            return await channel.test()
        return False

    async def test_all_channels(self) -> dict[str, bool]:
        """Test all alert channels."""
        results: dict[str, bool] = {}
        for name, channel in self._channels.items():
            results[name] = await channel.test()
        return results


# Global alert manager instance
_default_alert_manager: Optional[AlertManager] = None


def get_default_alert_manager() -> AlertManager:
    """Get or create the default alert manager."""
    global _default_alert_manager
    if _default_alert_manager is None:
        _default_alert_manager = AlertManager()
    return _default_alert_manager


def set_default_alert_manager(manager: Optional[AlertManager]) -> None:
    """Set the default alert manager."""
    global _default_alert_manager
    _default_alert_manager = manager
