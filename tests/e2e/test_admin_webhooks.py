"""E2E Tests: Webhooks, Admin & Observability (Phases 5 & 6)."""

import pytest
from starlette.testclient import TestClient


@pytest.mark.e2e
class TestWebhooks:
    """Test webhook functionality."""

    def test_create_webhook(self, client: TestClient):
        """Can create a webhook."""
        resp = client.post("/api/v1/webhooks", json={
            "url": "https://example.com/webhook",
            "events": ["agent.spawn", "agent.stop"]
        })
        
        assert resp.status_code == 200
        data = resp.json()
        assert "webhook_id" in data
        assert data["url"] == "https://example.com/webhook"
        assert data["events"] == ["agent.spawn", "agent.stop"]
        assert "secret" in data  # Secret only shown on creation
        assert data["active"] is True

    def test_list_webhooks(self, client: TestClient):
        """Can list webhooks (without secrets)."""
        # Create webhook
        client.post("/api/v1/webhooks", json={
            "url": "https://example.com/webhook1",
            "events": ["agent.spawn"]
        })
        
        resp = client.get("/api/v1/webhooks")
        
        assert resp.status_code == 200
        data = resp.json()
        assert "webhooks" in data
        # Verify secrets are not exposed
        for webhook in data["webhooks"]:
            assert "secret" not in webhook

    def test_get_webhook(self, client: TestClient):
        """Can get a specific webhook."""
        # Create webhook
        create_resp = client.post("/api/v1/webhooks", json={
            "url": "https://example.com/webhook-get",
            "events": ["agent.stop"]
        })
        webhook_id = create_resp.json()["webhook_id"]
        
        resp = client.get(f"/api/v1/webhooks/{webhook_id}")
        
        assert resp.status_code == 200
        data = resp.json()
        assert data["webhook_id"] == webhook_id
        assert "secret" not in data  # Secret not exposed

    def test_get_webhook_not_found(self, client: TestClient):
        """Getting non-existent webhook returns 404."""
        resp = client.get("/api/v1/webhooks/non-existent")
        
        assert resp.status_code == 404

    def test_delete_webhook(self, client: TestClient):
        """Can delete a webhook."""
        # Create webhook
        create_resp = client.post("/api/v1/webhooks", json={
            "url": "https://example.com/webhook-delete"
        })
        webhook_id = create_resp.json()["webhook_id"]
        
        resp = client.delete(f"/api/v1/webhooks/{webhook_id}")
        
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True


@pytest.mark.e2e
class TestAdminAuditLogs:
    """Test admin audit log functionality."""

    def test_list_audit_logs_admin_only(self, client: TestClient):
        """Audit logs require admin role."""
        # This test uses the default client which has admin role
        resp = client.get("/api/v1/audit/logs")
        
        assert resp.status_code == 200
        data = resp.json()
        assert "logs" in data
        assert "total" in data

    def test_audit_logs_filter_by_action(self, client: TestClient):
        """Can filter audit logs by action."""
        # Create some activity
        client.post("/api/v1/agents", json={"name": "audit-test"})
        
        resp = client.get("/api/v1/audit/logs?action=create")
        
        assert resp.status_code == 200
        data = resp.json()
        # Should find at least our agent creation
        create_logs = [l for l in data["logs"] if l.get("action") == "create"]
        assert len(create_logs) >= 1

    def test_audit_logs_pagination(self, client: TestClient):
        """Audit logs support pagination."""
        resp = client.get("/api/v1/audit/logs?limit=5&offset=0")
        
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 5
        assert data["offset"] == 0
        assert len(data["logs"]) <= 5

    def test_audit_logs_summary(self, client: TestClient):
        """Can get audit log summary."""
        resp = client.get("/api/v1/audit/logs/summary")
        
        assert resp.status_code == 200
        data = resp.json()
        assert "total_logs" in data
        assert "actions" in data
        assert "outcomes" in data
        assert "last_24h" in data


@pytest.mark.e2e
class TestMetrics:
    """Test metrics endpoints."""

    def test_get_system_metrics(self, client: TestClient):
        """Can get system metrics."""
        # Create some data
        client.post("/api/v1/agents", json={"name": "metrics-test", "model": "claude"})
        
        resp = client.get("/api/v1/metrics")
        
        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data
        assert "memory" in data
        assert "templates" in data
        assert "workflows" in data
        assert "webhooks" in data
        assert "timestamp" in data
        
        # Check agent metrics
        assert "total" in data["agents"]
        assert "by_status" in data["agents"]
        assert "by_model" in data["agents"]

    def test_get_agent_metrics(self, client: TestClient):
        """Can get metrics for a specific agent."""
        # Create agent
        agent = client.post("/api/v1/agents", json={"name": "agent-metrics-test"}).json()
        
        resp = client.get(f"/api/v1/metrics/agents/{agent['agent_id']}")
        
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == agent["agent_id"]
        assert "name" in data
        assert "status" in data
        assert "created_at" in data

    def test_get_agent_metrics_not_found(self, client: TestClient):
        """Getting metrics for non-existent agent returns 404."""
        resp = client.get("/api/v1/metrics/agents/non-existent")
        
        assert resp.status_code == 404


@pytest.mark.e2e
class TestRateLimits:
    """Test rate limit functionality."""

    def test_get_rate_limits_admin_only(self, client: TestClient):
        """Rate limits require admin role."""
        resp = client.get("/api/v1/rate-limits")
        
        assert resp.status_code == 200
        data = resp.json()
        assert "default" in data
        assert "custom" in data
        
        # Check default limits
        assert "requests_per_minute" in data["default"]
        assert "concurrent_agents" in data["default"]
        assert "memory_quota_mb" in data["default"]

    def test_set_rate_limit(self, client: TestClient):
        """Can set custom rate limits."""
        resp = client.post("/api/v1/rate-limits", json={
            "api_key": "test-key-123",
            "requests_per_minute": 200,
            "concurrent_agents": 20,
            "memory_quota_mb": 2048
        })
        
        assert resp.status_code == 200
        data = resp.json()
        assert "api_key" in data
        assert data["limits"]["requests_per_minute"] == 200
        assert data["limits"]["concurrent_agents"] == 20

    def test_set_rate_limit_missing_key(self, client: TestClient):
        """Setting rate limit without api_key returns error."""
        resp = client.post("/api/v1/rate-limits", json={
            "requests_per_minute": 100
        })
        
        assert resp.status_code == 400
        assert "api_key is required" in resp.json()["detail"]

    def test_delete_rate_limit(self, client: TestClient):
        """Can delete custom rate limits."""
        # Set a rate limit first
        client.post("/api/v1/rate-limits", json={
            "api_key": "delete-test-key",
            "requests_per_minute": 50
        })
        
        resp = client.delete("/api/v1/rate-limits/delete-test-key")
        
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
