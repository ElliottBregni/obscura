"""
Tests for sdk.auth.middleware -- JWT validation, JWKS caching, error handling.

Uses python-jose to forge test JWTs signed with an RSA key pair generated
at test time, simulating the Zitadel JWKS flow without a running server.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jose import jwt as jose_jwt

from sdk.auth.middleware import (
    JWKSCache,
    JWTAuthMiddleware,
    _extract_roles,
    _detect_token_type,
    decode_and_validate,
)
from sdk.auth.models import AuthenticatedUser

# ---------------------------------------------------------------------------
# Test RSA key pair (generated once per module)
# ---------------------------------------------------------------------------

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_key = _private_key.public_key()

_private_pem = _private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()

_public_pem = _public_key.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()

# Build a JWKS-like structure from the public key
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
import base64


def _int_to_base64url(n: int) -> str:
    byte_length = (n.bit_length() + 7) // 8
    b = n.to_bytes(byte_length, byteorder="big")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


_pub_numbers: RSAPublicNumbers = _public_key.public_numbers()
_TEST_KID = "test-key-1"
_TEST_JWKS: dict[str, Any] = {
    "keys": [
        {
            "kty": "RSA",
            "kid": _TEST_KID,
            "use": "sig",
            "alg": "RS256",
            "n": _int_to_base64url(_pub_numbers.n),
            "e": _int_to_base64url(_pub_numbers.e),
        }
    ]
}

_TEST_ISSUER = "http://zitadel.test"
_TEST_AUDIENCE = "obscura-test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _forge_token(
    *,
    sub: str = "user-123",
    email: str = "test@obscura.dev",
    roles: dict[str, Any] | None = None,
    issuer: str = _TEST_ISSUER,
    audience: str = _TEST_AUDIENCE,
    exp_offset: int = 3600,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Sign a JWT with the test private key."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": sub,
        "email": email,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + exp_offset,
    }
    if roles is not None:
        claims["urn:zitadel:iam:org:project:roles"] = roles
    if extra_claims:
        claims.update(extra_claims)

    return jose_jwt.encode(claims, _private_pem, algorithm="RS256", headers={"kid": _TEST_KID})


# ---------------------------------------------------------------------------
# Unit tests: role extraction
# ---------------------------------------------------------------------------

class TestExtractRoles:
    def test_extract_standard_roles(self) -> None:
        payload = {
            "urn:zitadel:iam:org:project:roles": {
                "admin": {"org1": "org1.zitadel.cloud"},
                "agent:copilot": {"org1": "org1.zitadel.cloud"},
            }
        }
        roles = _extract_roles(payload)
        assert set(roles) == {"admin", "agent:copilot"}

    def test_extract_no_roles(self) -> None:
        assert _extract_roles({}) == []

    def test_extract_non_dict_roles(self) -> None:
        assert _extract_roles({"urn:zitadel:iam:org:project:roles": "bad"}) == []


# ---------------------------------------------------------------------------
# Unit tests: token type detection
# ---------------------------------------------------------------------------

class TestDetectTokenType:
    def test_user_token(self) -> None:
        assert _detect_token_type({"sub": "u1", "email": "a@b.c"}) == "user"

    def test_service_token(self) -> None:
        assert _detect_token_type({"urn:zitadel:iam:user:type": "machine"}) == "service"

    def test_api_key_token(self) -> None:
        assert _detect_token_type({"api_key_id": "ak-1"}) == "api_key"


# ---------------------------------------------------------------------------
# JWKS cache
# ---------------------------------------------------------------------------

class TestJWKSCache:
    @pytest.mark.asyncio
    async def test_refresh_populates_keys(self) -> None:
        cache = JWKSCache("http://fake/.well-known/jwks.json", ttl=60)

        with patch("sdk.auth.middleware.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = _TEST_JWKS
            mock_client.get.return_value = mock_resp

            keys = await cache.get_keys()
            assert len(keys) == 1
            assert keys[0]["kid"] == _TEST_KID

    @pytest.mark.asyncio
    async def test_cache_staleness(self) -> None:
        cache = JWKSCache("http://fake/.well-known/jwks.json", ttl=0)
        assert cache.is_stale()

        # Force some keys in
        cache._keys = [{"kid": "x"}]
        cache._fetched_at = time.monotonic()

        # With ttl=0 it should immediately be stale
        assert cache.is_stale()

    def test_invalidate_resets_fetched_at(self) -> None:
        cache = JWKSCache("http://fake", ttl=300)
        cache._fetched_at = time.monotonic()
        cache.invalidate()
        assert cache._fetched_at == 0.0


# ---------------------------------------------------------------------------
# Token decoding
# ---------------------------------------------------------------------------

class TestDecodeAndValidate:
    @pytest.fixture
    def jwks_cache(self) -> JWKSCache:
        """Return a JWKSCache pre-loaded with the test JWKS."""
        cache = JWKSCache("http://fake", ttl=300)
        cache._keys = _TEST_JWKS["keys"]
        cache._fetched_at = time.monotonic()
        return cache

    @pytest.mark.asyncio
    async def test_valid_token(self, jwks_cache: JWKSCache) -> None:
        token = _forge_token(
            roles={"admin": {"org1": "o"}, "agent:read": {"org1": "o"}},
        )
        user = await decode_and_validate(
            token,
            jwks_cache=jwks_cache,
            issuer=_TEST_ISSUER,
            audience=_TEST_AUDIENCE,
        )
        assert isinstance(user, AuthenticatedUser)
        assert user.user_id == "user-123"
        assert user.email == "test@obscura.dev"
        assert "admin" in user.roles
        assert "agent:read" in user.roles
        assert user.token_type == "user"

    @pytest.mark.asyncio
    async def test_expired_token(self, jwks_cache: JWKSCache) -> None:
        token = _forge_token(exp_offset=-3600)  # expired 1h ago
        from jose.exceptions import ExpiredSignatureError

        with pytest.raises(ExpiredSignatureError):
            await decode_and_validate(
                token,
                jwks_cache=jwks_cache,
                issuer=_TEST_ISSUER,
                audience=_TEST_AUDIENCE,
            )

    @pytest.mark.asyncio
    async def test_wrong_audience(self, jwks_cache: JWKSCache) -> None:
        token = _forge_token(audience="wrong-aud")
        from jose import JWTError

        with pytest.raises(JWTError):
            await decode_and_validate(
                token,
                jwks_cache=jwks_cache,
                issuer=_TEST_ISSUER,
                audience=_TEST_AUDIENCE,
            )

    @pytest.mark.asyncio
    async def test_wrong_issuer(self, jwks_cache: JWKSCache) -> None:
        token = _forge_token(issuer="http://evil.test")
        from jose import JWTError

        with pytest.raises(JWTError):
            await decode_and_validate(
                token,
                jwks_cache=jwks_cache,
                issuer=_TEST_ISSUER,
                audience=_TEST_AUDIENCE,
            )

    @pytest.mark.asyncio
    async def test_tampered_token(self, jwks_cache: JWKSCache) -> None:
        token = _forge_token()
        # Tamper with the payload
        parts = token.split(".")
        parts[1] = parts[1][::-1]  # reverse the payload
        tampered = ".".join(parts)

        from jose import JWTError

        with pytest.raises((JWTError, Exception)):
            await decode_and_validate(
                tampered,
                jwks_cache=jwks_cache,
                issuer=_TEST_ISSUER,
                audience=_TEST_AUDIENCE,
            )

    @pytest.mark.asyncio
    async def test_service_token_type(self, jwks_cache: JWKSCache) -> None:
        token = _forge_token(
            extra_claims={"urn:zitadel:iam:user:type": "machine"},
        )
        user = await decode_and_validate(
            token,
            jwks_cache=jwks_cache,
            issuer=_TEST_ISSUER,
            audience=_TEST_AUDIENCE,
        )
        assert user.token_type == "service"


# ---------------------------------------------------------------------------
# Middleware integration (via starlette test client)
# ---------------------------------------------------------------------------

class TestJWTAuthMiddleware:
    """Test the middleware using starlette's TestClient."""

    @pytest.fixture
    def app(self) -> Any:
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def api_endpoint(request: Any) -> JSONResponse:
            user: AuthenticatedUser = request.state.user
            return JSONResponse({"user_id": user.user_id, "roles": user.roles})

        async def health(request: Any) -> JSONResponse:
            return JSONResponse({"status": "ok"})

        jwks_cache = JWKSCache("http://fake", ttl=300)
        jwks_cache._keys = _TEST_JWKS["keys"]
        jwks_cache._fetched_at = time.monotonic()

        starlette_app = Starlette(
            routes=[
                Route("/api/v1/test", api_endpoint),
                Route("/health", health),
            ],
        )
        starlette_app.add_middleware(
            JWTAuthMiddleware,
            jwks_cache=jwks_cache,
            issuer=_TEST_ISSUER,
            audience=_TEST_AUDIENCE,
        )
        return starlette_app

    def test_missing_auth_header_returns_401(self, app: Any) -> None:
        from starlette.testclient import TestClient

        client = TestClient(app)
        resp = client.get("/api/v1/test")
        assert resp.status_code == 401
        assert "Missing" in resp.json()["detail"]

    def test_malformed_auth_header_returns_401(self, app: Any) -> None:
        from starlette.testclient import TestClient

        client = TestClient(app)
        resp = client.get("/api/v1/test", headers={"Authorization": "Basic abc"})
        assert resp.status_code == 401

    def test_valid_token_passes(self, app: Any) -> None:
        from starlette.testclient import TestClient

        token = _forge_token(roles={"admin": {"o": "o"}})
        client = TestClient(app)
        resp = client.get("/api/v1/test", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "user-123"
        assert "admin" in data["roles"]

    def test_expired_token_returns_401(self, app: Any) -> None:
        from starlette.testclient import TestClient

        token = _forge_token(exp_offset=-3600)
        client = TestClient(app)
        resp = client.get("/api/v1/test", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"].lower()

    def test_health_skips_auth(self, app: Any) -> None:
        from starlette.testclient import TestClient

        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
