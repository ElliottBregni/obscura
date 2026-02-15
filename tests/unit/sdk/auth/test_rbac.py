"""
Tests for sdk.auth.rbac -- role-based access control dependencies.

Verifies role hierarchy, admin override, and proper 401/403 behaviour
when wired into a FastAPI application.
"""

from __future__ import annotations

import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from sdk.auth.middleware import JWKSCache, JWTAuthMiddleware
from sdk.auth.models import AuthenticatedUser
from sdk.auth.rbac import get_current_user, require_any_role, require_role

# Re-use the test key infrastructure from test_auth_middleware
from tests.unit.sdk.auth.test_auth_middleware import (
    _TEST_AUDIENCE,
    _TEST_ISSUER,
    _TEST_JWKS,
    _forge_token,
)


# ---------------------------------------------------------------------------
# AuthenticatedUser model tests
# ---------------------------------------------------------------------------


class TestAuthenticatedUser:
    def _make_user(self, roles: list[str]) -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id="u1",
            email="u@test.dev",
            roles=roles,
            org_id=None,
            token_type="user",
            raw_token="tok",
        )

    def test_has_role_exact_match(self) -> None:
        user = self._make_user(["agent:copilot"])
        assert user.has_role("agent:copilot")
        assert not user.has_role("admin")
        assert not user.has_role("sync:write")

    def test_has_role_admin_override(self) -> None:
        user = self._make_user(["admin"])
        assert user.has_role("agent:copilot")
        assert user.has_role("sync:write")
        assert user.has_role("sessions:manage")
        assert user.has_role("anything:unknown")

    def test_has_any_role(self) -> None:
        user = self._make_user(["agent:read", "sync:write"])
        assert user.has_any_role("agent:read", "sessions:manage")
        assert not user.has_any_role("admin", "sessions:manage")

    def test_has_any_role_admin_override(self) -> None:
        user = self._make_user(["admin"])
        assert user.has_any_role("foo", "bar", "baz")


# ---------------------------------------------------------------------------
# FastAPI dependency integration tests
# ---------------------------------------------------------------------------


def _create_test_app() -> FastAPI:
    """Build a FastAPI app with auth middleware and role-protected routes."""
    app = FastAPI()

    jwks_cache = JWKSCache("http://fake", ttl=300)
    jwks_cache._keys = _TEST_JWKS["keys"]
    jwks_cache._fetched_at = time.monotonic()

    app.add_middleware(
        JWTAuthMiddleware,
        jwks_cache=jwks_cache,
        issuer=_TEST_ISSUER,
        audience=_TEST_AUDIENCE,
    )

    @app.get("/api/v1/me")
    async def me(user: AuthenticatedUser = Depends(get_current_user)) -> dict:
        return {"user_id": user.user_id, "roles": user.roles}

    @app.get("/api/v1/admin-only")
    async def admin_only(
        user: AuthenticatedUser = Depends(require_role("admin")),
    ) -> dict:
        return {"ok": True}

    @app.post("/api/v1/sync")
    async def sync(
        user: AuthenticatedUser = Depends(require_role("sync:write")),
    ) -> dict:
        return {"synced": True}

    @app.post("/api/v1/agent")
    async def agent(
        user: AuthenticatedUser = Depends(
            require_any_role("agent:copilot", "agent:claude", "agent:read"),
        ),
    ) -> dict:
        return {"agent": True, "user_id": user.user_id}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_create_test_app())


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetCurrentUser:
    def test_returns_user_for_valid_token(self, client: TestClient) -> None:
        token = _forge_token(roles={"agent:read": {"o": "o"}})
        resp = client.get("/api/v1/me", headers=_bearer(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "user-123"
        assert "agent:read" in data["roles"]

    def test_401_without_token(self, client: TestClient) -> None:
        resp = client.get("/api/v1/me")
        assert resp.status_code == 401


class TestRequireRole:
    def test_admin_role_required_and_present(self, client: TestClient) -> None:
        token = _forge_token(roles={"admin": {"o": "o"}})
        resp = client.get("/api/v1/admin-only", headers=_bearer(token))
        assert resp.status_code == 200

    def test_admin_role_required_but_missing(self, client: TestClient) -> None:
        token = _forge_token(roles={"agent:read": {"o": "o"}})
        resp = client.get("/api/v1/admin-only", headers=_bearer(token))
        assert resp.status_code == 403
        assert "admin" in resp.json()["detail"]

    def test_sync_write_role(self, client: TestClient) -> None:
        token = _forge_token(roles={"sync:write": {"o": "o"}})
        resp = client.post("/api/v1/sync", headers=_bearer(token))
        assert resp.status_code == 200
        assert resp.json()["synced"] is True

    def test_admin_passes_any_role_check(self, client: TestClient) -> None:
        """Admin role should bypass any specific role requirement."""
        token = _forge_token(roles={"admin": {"o": "o"}})
        # admin should pass sync:write check
        resp = client.post("/api/v1/sync", headers=_bearer(token))
        assert resp.status_code == 200

    def test_insufficient_role_returns_403(self, client: TestClient) -> None:
        token = _forge_token(roles={"sessions:manage": {"o": "o"}})
        resp = client.post("/api/v1/sync", headers=_bearer(token))
        assert resp.status_code == 403


class TestRequireAnyRole:
    def test_any_agent_role_passes(self, client: TestClient) -> None:
        for role in ("agent:copilot", "agent:claude", "agent:read"):
            token = _forge_token(roles={role: {"o": "o"}})
            resp = client.post("/api/v1/agent", headers=_bearer(token))
            assert resp.status_code == 200, f"Failed for role: {role}"

    def test_non_agent_role_fails(self, client: TestClient) -> None:
        token = _forge_token(roles={"sync:write": {"o": "o"}})
        resp = client.post("/api/v1/agent", headers=_bearer(token))
        assert resp.status_code == 403

    def test_admin_passes(self, client: TestClient) -> None:
        token = _forge_token(roles={"admin": {"o": "o"}})
        resp = client.post("/api/v1/agent", headers=_bearer(token))
        assert resp.status_code == 200


class TestUnauthenticatedRoutes:
    def test_health_accessible_without_token(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
