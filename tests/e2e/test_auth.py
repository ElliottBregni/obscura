"""E2E Tests: API Key Authentication."""

import pytest


@pytest.mark.e2e
class TestAPIKeyAuth:
    """Test API key authentication flows."""

    def test_api_key_default_dev_key(self, client):
        """Default dev API key should work."""
        resp = client.get(
            "/api/v1/agents", headers={"X-API-Key": "obscura-dev-key-123"}
        )
        assert resp.status_code == 200

    def test_api_key_missing(self, client_no_auth_override):
        """Request without API key should fail when auth enabled."""
        # This client has auth enabled but no API key
        resp = client_no_auth_override.get("/api/v1/agents")
        assert resp.status_code == 401
        assert "Missing or malformed Authorization header" in resp.json()["detail"]

    def test_api_key_invalid(self, client_no_auth_override):
        """Invalid API key should fail."""
        resp = client_no_auth_override.get(
            "/api/v1/agents", headers={"X-API-Key": "invalid-key"}
        )
        assert resp.status_code == 401

    def test_api_key_spawn_agent(self, client):
        """Can spawn agent with API key."""
        resp = client.post(
            "/api/v1/agents",
            headers={"X-API-Key": "obscura-dev-key-123"},
            json={"name": "api-key-test", "model": "claude"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "agent_id" in data

        # Cleanup
        client.delete(
            f"/api/v1/agents/{data['agent_id']}",
            headers={"X-API-Key": "obscura-dev-key-123"},
        )

    def test_health_no_auth_needed(self, client_no_auth_override):
        """Health endpoint should work without auth."""
        resp = client_no_auth_override.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


@pytest.mark.e2e
class TestAuthBypass:
    """Test auth bypass when disabled."""

    def test_no_auth_when_disabled(self, client):
        """No auth needed when OBSCURA_AUTH_ENABLED=false."""
        # This client has auth disabled
        resp = client.get("/api/v1/agents")
        assert resp.status_code == 200

    def test_spawn_without_auth_when_disabled(self, client):
        """Can spawn agent without auth when disabled."""
        resp = client.post(
            "/api/v1/agents", json={"name": "no-auth-test", "model": "claude"}
        )
        assert resp.status_code == 200
        data = resp.json()

        # Cleanup
        client.delete(f"/api/v1/agents/{data['agent_id']}")
