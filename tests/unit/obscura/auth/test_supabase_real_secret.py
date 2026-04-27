"""End-to-end smoke test using the real SUPABASE_JWT_SECRET from .env.

Skipped automatically when ``SUPABASE_JWT_SECRET`` isn't set — CI without the
secret stays green. When the secret IS set we prove the full path:
we mint a token the way Supabase would, hand it to the middleware, and
confirm the user comes out with the expected roles.

This is the "verify your work" rule from the Supabase skill applied to the
JWT-validation surface.
"""

from __future__ import annotations

import os
import time

import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from obscura.auth.middleware import APIKeyAuthMiddleware
from obscura.auth.models import AuthenticatedUser
from obscura.auth.supabase import reset_verifier_for_tests


# Load .env values via python-dotenv if available, so running under `pytest`
# picks up the local secret without needing an exported env.
def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass


_load_env()


SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")
URL = os.environ.get("SUPABASE_URL", "")

skip_if_unconfigured = pytest.mark.skipif(
    not SECRET or not URL,
    reason="SUPABASE_JWT_SECRET / SUPABASE_URL not set",
)


def _build_app() -> FastAPI:
    app = FastAPI()

    class _Config:
        supabase_jwt_secret = SECRET
        supabase_jwks_url = ""
        supabase_audience = "authenticated"
        supabase_issuer = f"{URL.rstrip('/')}/auth/v1" if URL else ""
        supabase_url = URL

    app.state.config = _Config()
    app.add_middleware(APIKeyAuthMiddleware)

    @app.get("/api/v1/me")
    async def me(request: Request) -> JSONResponse:
        user: AuthenticatedUser = request.state.user
        return JSONResponse(
            content={
                "user_id": user.user_id,
                "email": user.email,
                "roles": list(user.roles),
                "token_type": user.token_type,
            },
        )

    return app


@skip_if_unconfigured
@pytest.mark.asyncio
async def test_real_secret_validates_minted_token() -> None:
    """Round-trip a Supabase-shaped token against the real project secret."""
    reset_verifier_for_tests()
    issuer = f"{URL.rstrip('/')}/auth/v1"
    now = int(time.time())
    payload = {
        "sub": "00000000-0000-0000-0000-000000000001",
        "email": "smoke@obscura.local",
        "aud": "authenticated",
        "iss": issuer,
        "iat": now,
        "exp": now + 600,
        "role": "authenticated",
        "app_metadata": {"roles": ["admin"]},
    }
    token = jwt.encode(payload, SECRET, algorithm="HS256")

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["user_id"] == "00000000-0000-0000-0000-000000000001"
    assert data["email"] == "smoke@obscura.local"
    assert data["token_type"] == "user"
    assert data["roles"] == ["admin"]


@skip_if_unconfigured
@pytest.mark.asyncio
async def test_real_secret_rejects_wrong_signature() -> None:
    """A token signed with a different secret must not authenticate."""
    reset_verifier_for_tests()
    issuer = f"{URL.rstrip('/')}/auth/v1"
    now = int(time.time())
    payload = {
        "sub": "00000000-0000-0000-0000-000000000002",
        "aud": "authenticated",
        "iss": issuer,
        "iat": now,
        "exp": now + 600,
        "role": "authenticated",
    }
    token = jwt.encode(payload, "definitely-not-the-real-secret-xxxxxxxxxxx", algorithm="HS256")

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 401
