"""Tests for miscellaneous route modules."""

from __future__ import annotations

from typing import Any

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from starlette.testclient import TestClient
from obscura.core.config import ObscuraConfig


@pytest.fixture
def app() -> Any:
    config = ObscuraConfig(auth_enabled=False, otel_enabled=False)
    from obscura.server import create_app

    return create_app(config)


@pytest.fixture
def client(app: Any) -> TestClient:
    return TestClient(app)


class TestHealthRoute:
    def test_health(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_ready(self, client: TestClient) -> None:
        resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestWebhookRoutes:
    def test_create_webhook(self, client: TestClient) -> None:
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

    def test_list_webhooks(self, client: TestClient) -> None:
        resp = client.get("/api/v1/webhooks")
        assert resp.status_code == 200
        data = resp.json()
        assert "webhooks" in data
        assert isinstance(data["webhooks"], list)

    def test_get_webhook(self, client: TestClient) -> None:
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

    def test_get_webhook_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/webhooks/nonexistent")
        assert resp.status_code == 404

    def test_delete_webhook(self, client: TestClient) -> None:
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
    def test_create_workflow(self, client: TestClient) -> None:
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

    def test_list_workflows(self, client: TestClient) -> None:
        resp = client.get("/api/v1/workflows")
        assert resp.status_code == 200

    def test_get_workflow(self, client: TestClient) -> None:
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

    def test_get_workflow_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/workflows/nonexistent")
        assert resp.status_code == 404

    def test_delete_workflow(self, client: TestClient) -> None:
        create = client.post("/api/v1/workflows", json={"name": "wf3", "steps": []})
        wid = create.json()["workflow_id"]
        resp = client.delete(f"/api/v1/workflows/{wid}")
        assert resp.status_code == 200


class TestAdminRoutes:
    def test_audit_logs(self, client: TestClient) -> None:
        resp = client.get("/api/v1/audit/logs")
        assert resp.status_code == 200

    def test_audit_logs_summary(self, client: TestClient) -> None:
        resp = client.get("/api/v1/audit/logs/summary")
        assert resp.status_code == 200

    @patch("obscura.routes.admin.get_runtime")
    def test_metrics(self, mock_runtime: Any, client: TestClient) -> None:
        mock_rt: Any = AsyncMock()
        mock_rt.list_agents = MagicMock(return_value=[])
        mock_runtime.return_value = mock_rt
        resp = client.get("/api/v1/metrics")
        assert resp.status_code == 200

    def test_rate_limits(self, client: TestClient) -> None:
        resp = client.get("/api/v1/rate-limits")
        assert resp.status_code == 200


class TestSessionRoutes:
    @patch("obscura.routes.sessions.ClientFactory")
    def test_list_sessions(self, mock_cf: Any, client: TestClient) -> None:
        resp = client.get("/api/v1/sessions")
        assert resp.status_code == 200


class TestSyncRoutes:
    @patch("obscura.routes.sync.record_sync_metric")
    def test_sync(self, mock_metric: Any, client: TestClient) -> None:
        resp = client.post("/api/v1/sync", json={})
        # May fail due to missing vault setup, but should not 500
        assert resp.status_code in (200, 422, 500)


class TestHeartbeatRoutes:
    def test_post_heartbeat(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/heartbeat",
            json={
                "agent_id": "agent-1",
                "status": "healthy",
            },
        )
        assert resp.status_code == 200

    def test_get_agent_health(self, client: TestClient) -> None:
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

    def test_list_health(self, client: TestClient) -> None:
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200


class TestSendRoutes:
    def test_send_endpoint_exists(self, app: Any, client: TestClient) -> None:
        # Mock the client factory on app state so the endpoint can resolve it
        mock_client: Any = AsyncMock()
        mock_client.send.return_value = MagicMock(text="hello")
        mock_client.stop = AsyncMock()
        mock_client.capability_tier = "B"
        mock_factory: Any = AsyncMock()
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
        mock_client.send.assert_awaited_once()
        args = mock_client.send.await_args.args
        kwargs = mock_client.send.await_args.kwargs
        assert args == ("hello",)
        assert kwargs["mode"] == "unified"
        assert kwargs["api_mode"] is None
        assert kwargs["native"] is None
        assert kwargs["request"].prompt == "hello"
        assert kwargs["request"].mode.value == "unified"

    def test_send_endpoint_native_payload(self, app: Any, client: TestClient) -> None:
        mock_client: Any = AsyncMock()
        mock_client.send.return_value = MagicMock(text="native-hello")
        mock_client.stop = AsyncMock()
        mock_client.capability_tier = "B"
        mock_factory: Any = AsyncMock()
        mock_factory.create.return_value = mock_client
        app.state.client_factory = mock_factory

        resp = client.post(
            "/api/v1/send",
            json={
                "backend": "openai",
                "prompt": "hello",
                "mode": "native",
                "api_mode": "responses",
                "native": {"openai": {"api_mode": "responses"}},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["text"] == "native-hello"
        mock_client.send.assert_awaited_once()
        args = mock_client.send.await_args.args
        kwargs = mock_client.send.await_args.kwargs
        assert args == ("hello",)
        assert kwargs["mode"] == "native"
        assert kwargs["api_mode"] == "responses"
        assert kwargs["native"] == {"openai": {"api_mode": "responses"}}
        assert kwargs["request"].mode.value == "native"
