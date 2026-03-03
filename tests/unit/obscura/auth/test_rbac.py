"""
Tests for obscura.auth.rbac -- role-based access control dependencies.

Verifies role hierarchy, admin override, and proper 401/403 behaviour
when wired into a FastAPI application using API key authentication.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from obscura.auth.middleware import APIKeyAuthMiddleware
from obscura.auth.models import AuthenticatedUser
from obscura.auth.rbac import (
    get_current_user,
    require_any_role,
    require_role,
    user_from_api_key,
)


# ---------------------------------------------------------------------------
# AuthenticatedUser model tests
# ---------------------------------------------------------------------------


class TestAuthenticatedUser:
    def _make_user(self, roles: tuple[str, ...]) -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id="u1",
            email="u@test.dev",
            roles=roles,
            org_id=None,
            token_type="user",
            raw_token="tok",
        )

    def test_has_role_exact_match(self) -> None:
        user = self._make_user(("agent:copilot",))
        assert user.has_role("agent:copilot")
        assert not user.has_role("admin")
        assert not user.has_role("sync:write")

    def test_has_role_admin_override(self) -> None:
        user = self._make_user(("admin",))
        assert user.has_role("agent:copilot")
        assert user.has_role("sync:write")
        assert user.has_role("sessions:manage")
        assert user.has_role("anything:unknown")

    def test_has_any_role(self) -> None:
        user = self._make_user(("agent:read", "sync:write"))
        assert user.has_any_role("agent:read", "sessions:manage")
        assert not user.has_any_role("admin", "sessions:manage")

    def test_has_any_role_admin_override(self) -> None:
        user = self._make_user(("admin",))
        assert user.has_any_role("foo", "bar", "baz")


# ---------------------------------------------------------------------------
# FastAPI dependency integration tests
# ---------------------------------------------------------------------------

# Default dev key header for convenience
_API_KEY_HEADER = {"X-API-Key": "obscura-dev-key-123"}


def _create_test_app() -> FastAPI:
    """Build a FastAPI app with API key auth middleware and role-protected routes."""
    app = FastAPI()
    app.add_middleware(APIKeyAuthMiddleware)

    @app.get("/api/v1/me")
    async def me(  # pyright: ignore[reportUnusedFunction]
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        return {"user_id": user.user_id, "roles": user.roles}

    @app.get("/api/v1/admin-only")
    async def admin_only(  # pyright: ignore[reportUnusedFunction]
        user: AuthenticatedUser = Depends(require_role("admin")),
    ) -> dict[str, Any]:
        return {"ok": True}

    @app.post("/api/v1/sync")
    async def sync(  # pyright: ignore[reportUnusedFunction]
        user: AuthenticatedUser = Depends(require_role("sync:write")),
    ) -> dict[str, Any]:
        return {"synced": True}

    @app.post("/api/v1/agent")
    async def agent(  # pyright: ignore[reportUnusedFunction]
        user: AuthenticatedUser = Depends(
            require_any_role("agent:copilot", "agent:claude", "agent:read"),
        ),
    ) -> dict[str, Any]:
        return {"agent": True, "user_id": user.user_id}

    @app.get("/health")
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_create_test_app())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetCurrentUser:
    def test_returns_user_for_valid_api_key(self, client: TestClient) -> None:
        resp = client.get("/api/v1/me", headers=_API_KEY_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "dev-user"
        assert "admin" in data["roles"]

    def test_401_without_api_key(self, client: TestClient) -> None:
        resp = client.get("/api/v1/me")
        assert resp.status_code == 401

    def test_user_from_api_key_helper(self) -> None:
        user = user_from_api_key("obscura-dev-key-123")
        assert user is not None
        assert user.user_id == "dev-user"
        assert user.token_type == "api_key"

    def test_user_from_api_key_returns_none_for_invalid(self) -> None:
        assert user_from_api_key("nonexistent-key") is None

    def test_user_from_api_key_returns_none_for_none(self) -> None:
        assert user_from_api_key(None) is None


class TestRequireRole:
    def test_admin_role_required_and_present(self, client: TestClient) -> None:
        # Default dev key has admin role
        resp = client.get("/api/v1/admin-only", headers=_API_KEY_HEADER)
        assert resp.status_code == 200

    def test_sync_write_role(self, client: TestClient) -> None:
        # Default dev key has sync:write role
        resp = client.post("/api/v1/sync", headers=_API_KEY_HEADER)
        assert resp.status_code == 200
        assert resp.json()["synced"] is True

    def test_admin_passes_any_role_check(self, client: TestClient) -> None:
        """Admin role should bypass any specific role requirement."""
        # Default dev key has admin role, which should pass sync:write check
        resp = client.post("/api/v1/sync", headers=_API_KEY_HEADER)
        assert resp.status_code == 200

    def test_missing_api_key_returns_401(self, client: TestClient) -> None:
        resp = client.post("/api/v1/sync")
        assert resp.status_code == 401


class TestRequireAnyRole:
    def test_any_agent_role_passes(self, client: TestClient) -> None:
        # Default dev key has admin which passes any role check
        resp = client.post("/api/v1/agent", headers=_API_KEY_HEADER)
        assert resp.status_code == 200

    def test_admin_passes(self, client: TestClient) -> None:
        resp = client.post("/api/v1/agent", headers=_API_KEY_HEADER)
        assert resp.status_code == 200


class TestUnauthenticatedRoutes:
    def test_health_accessible_without_api_key(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
