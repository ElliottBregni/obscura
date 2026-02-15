"""Tests for sdk.routes.webhooks — Webhook CRUD and test/trigger delivery."""

from __future__ import annotations

from typing import Any

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from starlette.testclient import TestClient
from sdk.config import ObscuraConfig


@pytest.fixture
def app() -> Any:
    config = ObscuraConfig(auth_enabled=False, otel_enabled=False)
    from sdk.server import create_app

    return create_app(config)


@pytest.fixture
def client(app: Any) -> TestClient:
    return TestClient(app)


class TestWebhookTest:
    @patch("httpx.AsyncClient")
    def test_webhook_test_success(self, mock_async_cls: Any, client: TestClient) -> None:
        # Create a webhook first
        create = client.post(
            "/api/v1/webhooks",
            json={
                "url": "https://example.com/test-hook",
                "events": ["agent.spawn"],
            },
        )
        wid = create.json()["webhook_id"]

        mock_response: Any = MagicMock()
        mock_response.status_code = 200
        mock_async_client: Any = AsyncMock()
        mock_async_client.post.return_value = mock_response
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_cls.return_value = mock_async_client

        resp = client.post(f"/api/v1/webhooks/{wid}/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status_code"] == 200

    def test_webhook_test_not_found(self, client: TestClient) -> None:
        resp = client.post("/api/v1/webhooks/nonexistent/test")
        assert resp.status_code == 404

    @patch("httpx.AsyncClient")
    def test_webhook_test_network_error(self, mock_async_cls: Any, client: TestClient) -> None:
        create = client.post(
            "/api/v1/webhooks",
            json={
                "url": "https://example.com/fail-hook",
                "events": ["agent.stop"],
            },
        )
        wid = create.json()["webhook_id"]

        mock_async_client: Any = AsyncMock()
        mock_async_client.post.side_effect = ConnectionError("refused")
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_cls.return_value = mock_async_client

        resp = client.post(f"/api/v1/webhooks/{wid}/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "error" in data


class TestTriggerWebhooks:
    @pytest.mark.asyncio
    @patch("httpx.AsyncClient")
    async def test_trigger_webhooks(self, mock_async_cls: Any) -> None:
        from sdk.routes.webhooks import get_webhooks_store, trigger_webhooks, WebhookConfig

        store = get_webhooks_store()
        # Clear all webhooks to avoid interference from other tests
        store.clear()
        store["test-wh"] = WebhookConfig(
            webhook_id="test-wh",
            url="https://example.com/hook",
            events=["agent.spawn"],
            secret="test-secret",
            active=True,
            created_by="test",
            created_at="2024-01-01T00:00:00Z",
        )

        mock_async_client: Any = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_cls.return_value = mock_async_client

        await trigger_webhooks("agent.spawn", {"user_id": "u1"})
        mock_async_client.post.assert_awaited_once()

        store.pop("test-wh", None)

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient")
    async def test_trigger_skips_inactive(self, mock_async_cls: Any) -> None:
        from sdk.routes.webhooks import get_webhooks_store, trigger_webhooks, WebhookConfig

        store = get_webhooks_store()
        store.clear()
        store["inactive-wh"] = WebhookConfig(
            webhook_id="inactive-wh",
            url="https://example.com/hook",
            events=["agent.spawn"],
            secret="s",
            active=False,
            created_by="test",
            created_at="2024-01-01T00:00:00Z",
        )

        mock_async_client: Any = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_cls.return_value = mock_async_client

        await trigger_webhooks("agent.spawn", {"user_id": "u1"})
        mock_async_client.post.assert_not_awaited()

        store.pop("inactive-wh", None)

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient")
    async def test_trigger_skips_wrong_event(self, mock_async_cls: Any) -> None:
        from sdk.routes.webhooks import get_webhooks_store, trigger_webhooks, WebhookConfig

        store = get_webhooks_store()
        store.clear()
        store["event-wh"] = WebhookConfig(
            webhook_id="event-wh",
            url="https://example.com/hook",
            events=["agent.stop"],
            secret="s",
            active=True,
            created_by="test",
            created_at="2024-01-01T00:00:00Z",
        )

        mock_async_client: Any = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_cls.return_value = mock_async_client

        await trigger_webhooks("agent.spawn", {"user_id": "u1"})
        mock_async_client.post.assert_not_awaited()

        store.pop("event-wh", None)
