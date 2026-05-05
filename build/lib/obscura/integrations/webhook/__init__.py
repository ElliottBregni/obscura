"""Generic outbound webhook channel adapter."""

from obscura.integrations.webhook.adapter import WebhookAdapter
from obscura.integrations.webhook.client import WebhookClient, WebhookDelivery
from obscura.integrations.webhook.state import WebhookState

__all__ = ["WebhookAdapter", "WebhookClient", "WebhookDelivery", "WebhookState"]
