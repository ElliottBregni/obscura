"""
obscura.auth.middleware -- JWT validation middleware for the FastAPI server.

Fetches JWKS from Zitadel's well-known endpoint, caches the key set,
validates JWT signature / issuer / audience / expiration, extracts
Zitadel project roles, and populates ``request.state.user`` with an
:class:`~obscura.auth.models.AuthenticatedUser`.
"""

from __future__ import annotations

import logging
import time
from typing import Any, override

import httpx

try:
    from jose import JWTError, jwt
    from jose.exceptions import ExpiredSignatureError

    _jose_available = True
except (
    Exception
):  # pragma: no cover - optional dependency may be missing in some environments
    # Provide fallbacks so the module can be imported even when the optional
    # `python-jose` dependency is not installed. Runtime checks will raise an
    # informative ImportError when JWT functionality is actually used.
    JWTError: type[Exception] = Exception
    ExpiredSignatureError: type[Exception] = Exception
    jwt = None
    _jose_available = False
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from obscura.auth.models import AuthenticatedUser

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JWKS cache
# ---------------------------------------------------------------------------

_ZITADEL_ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"

# How long (seconds) to cache the JWKS before re-fetching.
_JWKS_CACHE_TTL = 300  # 5 minutes


class JWKSCache:
    """Thread-safe, TTL-based cache for JSON Web Key Sets."""

    def __init__(self, jwks_uri: str, *, ttl: int = _JWKS_CACHE_TTL) -> None:
        self._jwks_uri = jwks_uri
        self._ttl = ttl
        self._keys: list[dict[str, Any]] = []
        self._fetched_at: float = 0.0

    @property
    def keys(self) -> list[dict[str, Any]]:
        return list(self._keys)

    @property
    def fetched_at(self) -> float:
        """Timestamp of the last JWKS fetch."""
        return self._fetched_at

    def clear(self) -> None:
        """Clear cached keys (testing/observability)."""
        self._keys = []
        self._fetched_at = 0.0

    def is_stale(self) -> bool:
        return time.monotonic() - self._fetched_at >= self._ttl

    async def get_keys(self) -> list[dict[str, Any]]:
        """Return cached keys, refreshing if the TTL has elapsed."""
        if not self._keys or self.is_stale():
            await self.refresh()
        return list(self._keys)

    async def refresh(self) -> None:
        """Fetch keys from the JWKS endpoint."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(self._jwks_uri)
                resp.raise_for_status()
                data = resp.json()
                self._keys = data.get("keys", [])
                self._fetched_at = time.monotonic()
                logger.debug(
                    "JWKS refreshed from %s (%d keys)", self._jwks_uri, len(self._keys)
                )
        except Exception:
            logger.exception("Failed to fetch JWKS from %s", self._jwks_uri)
            # Keep stale keys if we have them; raise if we have nothing
            if not self._keys:
                raise

    def invalidate(self) -> None:
        """Force the next ``get_keys`` call to re-fetch."""
        self._fetched_at = 0.0


# ---------------------------------------------------------------------------
# Token decoding
# ---------------------------------------------------------------------------


def extract_roles(payload: dict[str, Any]) -> list[str]:
    """Extract flat role list from Zitadel's nested roles claim.

    Zitadel encodes project roles as::

        "urn:zitadel:iam:org:project:roles": {
            "admin": {"<org_id>": "<org_domain>"},
            "agent:copilot": {"<org_id>": "<org_domain>"},
        }
    """
    roles_obj: dict[str, Any] | None = payload.get(_ZITADEL_ROLES_CLAIM)
    if isinstance(roles_obj, dict):
        return list(roles_obj.keys())
    return []


def detect_token_type(payload: dict[str, Any]) -> str:
    """Heuristic: detect whether the JWT represents a user, service account, or API key."""
    # Zitadel service users have urn:zitadel:iam:org:project:<id>:roles but no "email"
    if payload.get("urn:zitadel:iam:user:type") == "machine":
        return "service"
    if "api_key_id" in payload:
        return "api_key"
    return "user"


async def decode_and_validate(
    token: str,
    *,
    jwks_cache: JWKSCache,
    issuer: str,
    audience: str,
) -> AuthenticatedUser:
    """Validate a JWT and return the corresponding :class:`AuthenticatedUser`.

    Raises ``JWTError`` or ``ValueError`` on failure.
    """
    if jwt is None:
        raise ImportError(
            "Missing optional dependency 'python-jose'. Install with "
            "'pip install python-jose[cryptography]' or install the 'server' extras: "
            "'pip install .[server]'"
        )
    keys = await jwks_cache.get_keys()

    try:
        payload = jwt.decode(
            token,
            {"keys": keys},
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"verify_at_hash": False},
        )
    except ExpiredSignatureError:
        raise
    except JWTError:
        # The key might have rotated -- refresh once and retry
        jwks_cache.invalidate()
        keys = await jwks_cache.get_keys()
        payload = jwt.decode(
            token,
            {"keys": keys},
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"verify_at_hash": False},
        )

    roles = extract_roles(payload)
    token_type = detect_token_type(payload)

    return AuthenticatedUser(
        user_id=payload.get("sub", ""),
        email=payload.get("email", ""),
        roles=tuple(roles),
        org_id=payload.get("urn:zitadel:iam:org:id"),
        token_type=token_type,
        raw_token=token,
    )


# ---------------------------------------------------------------------------
# Starlette middleware
# ---------------------------------------------------------------------------


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """Validates JWTs on ``/api/`` routes and sets ``request.state.user``.

    Requests to paths that do not start with ``/api/`` are passed through
    without authentication (e.g. ``/health``, ``/ready``).
    """

    def __init__(
        self,
        app: Any,
        *,
        jwks_cache: JWKSCache,
        issuer: str,
        audience: str,
    ) -> None:
        super().__init__(app)
        self._jwks_cache = jwks_cache
        self._issuer = issuer
        self._audience = audience

    @override
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip auth for non-API routes
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            _emit_auth_audit(
                request.url.path, "anonymous", "", "denied", reason="missing_header"
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or malformed Authorization header"},
            )

        token = auth_header.removeprefix("Bearer ").strip()
        try:
            user = await decode_and_validate(
                token,
                jwks_cache=self._jwks_cache,
                issuer=self._issuer,
                audience=self._audience,
            )
        except ExpiredSignatureError:
            _emit_auth_audit(
                request.url.path, "unknown", "", "denied", reason="expired_token"
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Token has expired"},
            )
        except ImportError as exc:
            logger.error("JWT library missing: %s", exc)
            return JSONResponse(
                status_code=500,
                content={
                    "detail": (
                        "Server misconfigured: missing dependency 'python-jose'. "
                        "Install with 'pip install python-jose[cryptography]' or 'pip install .[server]'"
                    )
                },
            )
        except (JWTError, ValueError) as exc:
            logger.warning("JWT validation failed: %s", str(exc))
            _emit_auth_audit(
                request.url.path, "unknown", "", "denied", reason="invalid_token"
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid token"},
            )

        request.state.user = user
        return await call_next(request)


# ---------------------------------------------------------------------------
# Auth audit helper
# ---------------------------------------------------------------------------


def _emit_auth_audit(
    path: str,
    user_id: str,
    email: str,
    outcome: str,
    **details: Any,
) -> None:
    """Emit an audit event for auth decisions (best-effort)."""
    try:
        from obscura.telemetry.audit import AuditEvent, emit_audit_event

        emit_audit_event(
            AuditEvent(
                event_type="auth.validate",
                user_id=user_id,
                user_email=email,
                resource=f"endpoint:{path}",
                action="authenticate",
                outcome=outcome,
                details=details,
            )
        )
    except Exception:
        pass
