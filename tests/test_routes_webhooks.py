"""Tests for sdk.routes.webhooks — Webhook CRUD and test/trigger delivery."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from starlette.testclient import TestClient
from sdk.config import ObscuraConfig


@pytest.fixture
def app():
    config = ObscuraConfig(auth_enabled=False, otel_enabled=False)
    from sdk.server import create_app
    return create_app(config)


@pytest.fixture
def client(app):
    return TestClient(app)


class TestWebhookTest:
    @patch("httpx.AsyncClient")
    def test_webhook_test_success(self, mock_async_cls, client):
        # Create a webhook first
        create = client.post("/api/v1/webhooks", json={
            "url": "https://example.com/test-hook",
            "events": ["agent.spawn"],
        })
        wid = create.json()["webhook_id"]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_async_client = AsyncMock()
        mock_async_client.post.return_value = mock_response
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_cls.return_value = mock_async_client

        resp = client.post(f"/api/v1/webhooks/{wid}/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status_code"] == 200

    def test_webhook_test_not_found(self, client):
        resp = client.post("/api/v1/webhooks/nonexistent/test")
        assert resp.status_code == 404

    @patch("httpx.AsyncClient")
    def test_webhook_test_network_error(self, mock_async_cls, client):
        create = client.post("/api/v1/webhooks", json={
            "url": "https://example.com/fail-hook",
            "events": ["agent.stop"],
        })
        wid = create.json()["webhook_id"]

        mock_async_client = AsyncMock()
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
    async def test_trigger_webhooks(self, mock_async_cls):
        from sdk.routes.webhooks import _webhooks, trigger_webhooks

        # Clear all webhooks to avoid interference from other tests
        _webhooks.clear()
        _webhooks["test-wh"] = {
            "webhook_id": "test-wh",
            "url": "https://example.com/hook",
            "events": ["agent.spawn"],
            "secret": "test-secret",
            "active": True,
        }

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_cls.return_value = mock_async_client

        await trigger_webhooks("agent.spawn", {"user_id": "u1"})
        mock_async_client.post.assert_awaited_once()

        _webhooks.pop("test-wh", None)

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient")
    async def test_trigger_skips_inactive(self, mock_async_cls):
        from sdk.routes.webhooks import _webhooks, trigger_webhooks

        _webhooks.clear()
        _webhooks["inactive-wh"] = {
            "webhook_id": "inactive-wh",
            "url": "https://example.com/hook",
            "events": ["agent.spawn"],
            "secret": "s",
            "active": False,
        }

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_cls.return_value = mock_async_client

        await trigger_webhooks("agent.spawn", {"user_id": "u1"})
        mock_async_client.post.assert_not_awaited()

        _webhooks.pop("inactive-wh", None)

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient")
    async def test_trigger_skips_wrong_event(self, mock_async_cls):
        from sdk.routes.webhooks import _webhooks, trigger_webhooks

        _webhooks.clear()
        _webhooks["event-wh"] = {
            "webhook_id": "event-wh",
            "url": "https://example.com/hook",
            "events": ["agent.stop"],
            "secret": "s",
            "active": True,
        }

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_cls.return_value = mock_async_client

        await trigger_webhooks("agent.spawn", {"user_id": "u1"})
        mock_async_client.post.assert_not_awaited()

        _webhooks.pop("event-wh", None)
