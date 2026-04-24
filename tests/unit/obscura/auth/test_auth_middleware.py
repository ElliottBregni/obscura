"""Tests for API key + Supabase bearer authentication middleware."""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from obscura.auth import middleware as auth_middleware
from obscura.auth.middleware import APIKeyAuthMiddleware
from obscura.auth.rbac import _load_api_keys
from obscura.auth.supabase import reset_verifier_for_tests


@pytest.fixture(autouse=True)
def _enable_dev_mode(monkeypatch: pytest.MonkeyPatch):
    """Enable OBSCURA_DEV_MODE so the dev API key loads for middleware tests."""
    monkeypatch.setenv("OBSCURA_DEV_MODE", "true")
    monkeypatch.delenv("OBSCURA_API_KEYS", raising=False)
    _load_api_keys()
    yield
    monkeypatch.delenv("OBSCURA_DEV_MODE", raising=False)
    _load_api_keys()

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser


_HS_SECRET = "test-supabase-secret-at-least-32-bytes-long"
_ISSUER = "https://test.supabase.co/auth/v1"


def _mint(
    *,
    sub: str = "user-123",
    email: str = "user@example.com",
    roles: list[str] | None = None,
    exp_offset: int = 3600,
    audience: str = "authenticated",
    issuer: str = _ISSUER,
    secret: str = _HS_SECRET,
) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "sub": sub,
        "email": email,
        "aud": audience,
        "iss": issuer,
        "iat": now,
        "exp": now + exp_offset,
        "role": "authenticated",
    }
    if roles is not None:
        payload["app_metadata"] = {"roles": roles}
    return jwt.encode(payload, secret, algorithm="HS256")


def _create_test_app(*, supabase: bool = False) -> FastAPI:
    """Create a minimal FastAPI app with APIKeyAuthMiddleware."""
    app = FastAPI()

    class _Config:
        supabase_jwt_secret = _HS_SECRET if supabase else ""
        supabase_jwks_url = ""
        supabase_audience = "authenticated"
        supabase_issuer = _ISSUER
        supabase_url = "https://test.supabase.co"

    app.state.config = _Config()
    app.add_middleware(APIKeyAuthMiddleware)

    @app.get("/api/v1/test")
    async def api_test(request: Request) -> JSONResponse:
        user: AuthenticatedUser = request.state.user
        return JSONResponse(
            content={
                "user_id": user.user_id,
                "roles": list(user.roles),
                "token_type": user.token_type,
            },
        )

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(content={"status": "ok"})

    return app


class TestAPIKeyAuthMiddleware:
    @pytest.fixture(autouse=True)
    def _reset_provisioned_users(self) -> None:
        auth_middleware._PROVISIONED_USERS.clear()

    @pytest.mark.asyncio
    async def test_valid_api_key(self) -> None:
        app = _create_test_app()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/api/v1/test",
                headers={"X-API-Key": "obscura-dev-key-123"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["user_id"] == "dev-user"
            assert "agent:read" in data["roles"]
            assert data["token_type"] == "api_key"

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_401(self) -> None:
        app = _create_test_app()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/api/v1/test")
            assert resp.status_code == 401
            assert "Missing or invalid credentials" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_invalid_api_key_returns_401(self) -> None:
        app = _create_test_app()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/api/v1/test", headers={"X-API-Key": "bad-key"})
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_non_api_route_passes_through(self) -> None:
        app = _create_test_app()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_supabase_bearer_accepted(self) -> None:
        reset_verifier_for_tests()
        app = _create_test_app(supabase=True)
        token = _mint(roles=["agent:copilot"])
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/api/v1/test",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["user_id"] == "user-123"
            assert data["token_type"] == "user"
            assert "agent:copilot" in data["roles"]

    @pytest.mark.asyncio
    async def test_supabase_default_role(self) -> None:
        reset_verifier_for_tests()
        app = _create_test_app(supabase=True)
        token = _mint()  # no roles
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/api/v1/test",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            assert resp.json()["roles"] == ["agent:read"]

    @pytest.mark.asyncio
    async def test_supabase_expired_rejected(self) -> None:
        reset_verifier_for_tests()
        app = _create_test_app(supabase=True)
        token = _mint(exp_offset=-60)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/api/v1/test",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_supabase_bad_signature_rejected(self) -> None:
        reset_verifier_for_tests()
        app = _create_test_app(supabase=True)
        token = _mint(secret="wrong-secret-also-at-least-32-bytes-long")
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/api/v1/test",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_supabase_roles_filtered_to_valid_set(self) -> None:
        """Unknown roles in app_metadata are stripped — prevents scope inflation."""
        reset_verifier_for_tests()
        app = _create_test_app(supabase=True)
        token = _mint(roles=["agent:read", "not:a:real:role", "admin"])
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/api/v1/test",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            assert set(resp.json()["roles"]) == {"agent:read", "admin"}

    @pytest.mark.asyncio
    async def test_api_key_still_works_when_supabase_configured(self) -> None:
        reset_verifier_for_tests()
        app = _create_test_app(supabase=True)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/api/v1/test",
                headers={"X-API-Key": "obscura-dev-key-123"},
            )
            assert resp.status_code == 200
            assert resp.json()["token_type"] == "api_key"

    @pytest.mark.asyncio
    async def test_bearer_ignored_when_supabase_not_configured(self) -> None:
        reset_verifier_for_tests()
        app = _create_test_app(supabase=False)
        token = _mint()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/api/v1/test",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_auth_failures_rate_limited_per_ip(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After enough bad-auth attempts from the same IP, return 429."""
        import obscura.auth.middleware as mw

        monkeypatch.setattr(mw, "_AUTH_FAILURE_THRESHOLD", 3)
        app = _create_test_app()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            for _ in range(3):
                r = await client.get("/api/v1/test")
                assert r.status_code == 401
            r = await client.get("/api/v1/test")
            assert r.status_code == 429
            assert "auth failures" in r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_dev_key_not_loaded_when_dev_mode_off(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Production default: no dev key, no API-key auth path at all."""
        monkeypatch.delenv("OBSCURA_DEV_MODE", raising=False)
        monkeypatch.delenv("OBSCURA_API_KEYS", raising=False)
        _load_api_keys()
        app = _create_test_app()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/api/v1/test",
                headers={"X-API-Key": "obscura-dev-key-123"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_custom_api_key_from_env(self) -> None:
        """API keys loaded from OBSCURA_API_KEYS env var."""
        from obscura.auth.rbac import _load_api_keys

        with patch.dict(os.environ, {"OBSCURA_API_KEYS": "mykey:testuser:admin"}):
            _load_api_keys()
            try:
                app = _create_test_app()
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                ) as client:
                    resp = await client.get(
                        "/api/v1/test",
                        headers={"X-API-Key": "mykey"},
                    )
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["user_id"] == "testuser"
                    assert "admin" in data["roles"]
            finally:
                # Restore default keys
                with patch.dict(os.environ, {}, clear=True):
                    os.environ.pop("OBSCURA_API_KEYS", None)
                    _load_api_keys()
