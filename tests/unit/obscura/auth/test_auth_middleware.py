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

from obscura.auth.middleware import APIKeyAuthMiddleware
from obscura.auth.supabase import reset_verifier_for_tests

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
    """Mint a signed HS256 token that imitates a Supabase session JWT."""
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
        """A valid Supabase HS256 token authenticates without an API key."""
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
    async def test_supabase_default_role_when_metadata_missing(self) -> None:
        """Signed-in users without app_metadata.roles get agent:read by default."""
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
    async def test_supabase_expired_token_rejected(self) -> None:
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
        """Unknown roles in app_metadata are stripped (prevents scope inflation)."""
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
            roles = set(resp.json()["roles"])
            assert roles == {"agent:read", "admin"}

    @pytest.mark.asyncio
    async def test_api_key_still_bypasses_when_supabase_configured(self) -> None:
        """The X-API-Key path keeps working with Supabase enabled (local bypass)."""
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
        """Without SUPABASE_* vars, bearer tokens are treated as absent."""
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
