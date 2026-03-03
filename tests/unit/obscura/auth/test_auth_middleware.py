"""Tests for API key authentication middleware."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from obscura.auth.middleware import APIKeyAuthMiddleware
from obscura.auth.models import AuthenticatedUser


def _create_test_app() -> FastAPI:
    """Create a minimal FastAPI app with APIKeyAuthMiddleware."""
    app = FastAPI()
    app.add_middleware(APIKeyAuthMiddleware)

    @app.get("/api/v1/test")
    async def api_test(request: Request) -> JSONResponse:
        user: AuthenticatedUser = request.state.user
        return JSONResponse(content={
            "user_id": user.user_id,
            "roles": list(user.roles),
            "token_type": user.token_type,
        })

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(content={"status": "ok"})

    return app


class TestAPIKeyAuthMiddleware:
    @pytest.mark.asyncio
    async def test_valid_api_key(self) -> None:
        app = _create_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/test", headers={"X-API-Key": "obscura-dev-key-123"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["user_id"] == "dev-user"
            assert "admin" in data["roles"]
            assert data["token_type"] == "api_key"

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_401(self) -> None:
        app = _create_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/test")
            assert resp.status_code == 401
            assert "Missing or invalid API key" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_invalid_api_key_returns_401(self) -> None:
        app = _create_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/test", headers={"X-API-Key": "bad-key"})
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_non_api_route_passes_through(self) -> None:
        app = _create_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_custom_api_key_from_env(self) -> None:
        """API keys loaded from OBSCURA_API_KEYS env var."""
        from obscura.auth.rbac import _load_api_keys

        with patch.dict(os.environ, {"OBSCURA_API_KEYS": "mykey:testuser:admin"}):
            _load_api_keys()
            try:
                app = _create_test_app()
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.get("/api/v1/test", headers={"X-API-Key": "mykey"})
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["user_id"] == "testuser"
                    assert "admin" in data["roles"]
            finally:
                # Restore default keys
                with patch.dict(os.environ, {}, clear=True):
                    os.environ.pop("OBSCURA_API_KEYS", None)
                    _load_api_keys()
