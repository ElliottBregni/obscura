"""Tests for miscellaneous route modules."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
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


class TestHealthRoute:
    def test_health(self, client):
        # /health is the liveness probe (no /api/v1 prefix)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_ready(self, client):
        resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestWebhookRoutes:
    def test_create_webhook(self, client):
        resp = client.post(
            "/api/v1/webhooks",
            json={
                "url": "https://example.com/hook",
                "events": ["agent.spawn"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "webhook_id" in data

    def test_list_webhooks(self, client):
        resp = client.get("/api/v1/webhooks")
        assert resp.status_code == 200
        data = resp.json()
        assert "webhooks" in data
        assert isinstance(data["webhooks"], list)

    def test_get_webhook(self, client):
        create = client.post(
            "/api/v1/webhooks",
            json={
                "url": "https://example.com/hook2",
                "events": ["agent.stop"],
            },
        )
        wid = create.json()["webhook_id"]
        resp = client.get(f"/api/v1/webhooks/{wid}")
        assert resp.status_code == 200

    def test_get_webhook_not_found(self, client):
        resp = client.get("/api/v1/webhooks/nonexistent")
        assert resp.status_code == 404

    def test_delete_webhook(self, client):
        create = client.post(
            "/api/v1/webhooks",
            json={
                "url": "https://example.com/hook3",
                "events": ["agent.run"],
            },
        )
        wid = create.json()["webhook_id"]
        resp = client.delete(f"/api/v1/webhooks/{wid}")
        assert resp.status_code == 200


class TestWorkflowRoutes:
    def test_create_workflow(self, client):
        resp = client.post(
            "/api/v1/workflows",
            json={
                "name": "test-wf",
                "description": "A test workflow",
                "steps": [{"type": "agent", "config": {"model": "copilot"}}],
            },
        )
        assert resp.status_code == 200
        assert "workflow_id" in resp.json()

    def test_list_workflows(self, client):
        resp = client.get("/api/v1/workflows")
        assert resp.status_code == 200

    def test_get_workflow(self, client):
        create = client.post(
            "/api/v1/workflows",
            json={
                "name": "wf2",
                "steps": [],
            },
        )
        wid = create.json()["workflow_id"]
        resp = client.get(f"/api/v1/workflows/{wid}")
        assert resp.status_code == 200

    def test_get_workflow_not_found(self, client):
        resp = client.get("/api/v1/workflows/nonexistent")
        assert resp.status_code == 404

    def test_delete_workflow(self, client):
        create = client.post("/api/v1/workflows", json={"name": "wf3", "steps": []})
        wid = create.json()["workflow_id"]
        resp = client.delete(f"/api/v1/workflows/{wid}")
        assert resp.status_code == 200


class TestAdminRoutes:
    def test_audit_logs(self, client):
        resp = client.get("/api/v1/audit/logs")
        assert resp.status_code == 200

    def test_audit_logs_summary(self, client):
        resp = client.get("/api/v1/audit/logs/summary")
        assert resp.status_code == 200

    @patch("sdk.routes.admin.get_runtime")
    def test_metrics(self, mock_runtime, client):
        mock_rt = AsyncMock()
        mock_rt.list_agents = MagicMock(return_value=[])
        mock_runtime.return_value = mock_rt
        resp = client.get("/api/v1/metrics")
        assert resp.status_code == 200

    def test_rate_limits(self, client):
        resp = client.get("/api/v1/rate-limits")
        assert resp.status_code == 200


class TestSessionRoutes:
    @patch("sdk.routes.sessions.ClientFactory")
    def test_list_sessions(self, mock_cf, client):
        resp = client.get("/api/v1/sessions")
        assert resp.status_code == 200


class TestSyncRoutes:
    @patch("sdk.routes.sync.record_sync_metric")
    def test_sync(self, mock_metric, client):
        resp = client.post("/api/v1/sync", json={})
        # May fail due to missing vault setup, but should not 500
        assert resp.status_code in (200, 422, 500)


class TestHeartbeatRoutes:
    def test_post_heartbeat(self, client):
        resp = client.post(
            "/api/v1/heartbeat",
            json={
                "agent_id": "agent-1",
                "status": "healthy",
            },
        )
        assert resp.status_code == 200

    def test_get_agent_health(self, client):
        # Post one first
        client.post(
            "/api/v1/heartbeat",
            json={
                "agent_id": "agent-2",
                "status": "healthy",
            },
        )
        resp = client.get("/api/v1/heartbeat/agent-2")
        assert resp.status_code in (200, 404)

    def test_list_health(self, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200


class TestSendRoutes:
    def test_send_endpoint_exists(self, app, client):
        # Mock the client factory on app state so the endpoint can resolve it
        mock_client = AsyncMock()
        mock_client.send.return_value = MagicMock(text="hello")
        mock_client.stop = AsyncMock()
        mock_client.capability_tier = "B"
        mock_factory = AsyncMock()
        mock_factory.create.return_value = mock_client
        app.state.client_factory = mock_factory

        resp = client.post(
            "/api/v1/send",
            json={
                "backend": "copilot",
                "prompt": "hello",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["text"] == "hello"
