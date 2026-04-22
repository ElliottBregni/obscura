"""obscura.auth.supabase -- Supabase OAuth JWT validation.

Validates bearer tokens minted by a Supabase Auth project and projects them
into the Obscura :class:`~obscura.auth.models.AuthenticatedUser` shape.

Two verification modes are supported:

* **HS256 shared secret** — set ``SUPABASE_JWT_SECRET`` to the project's JWT
  secret. This is the default for Supabase's legacy tokens.
* **RS256 via JWKS** — set ``SUPABASE_JWKS_URL`` (e.g.
  ``https://<project>.supabase.co/auth/v1/.well-known/jwks.json``). The first
  request fetches and caches the key set.

Role and org mapping:

* Roles are read from ``app_metadata.roles`` (list[str]) if present, otherwise
  fall back to a single string in ``app_metadata.role`` or the top-level
  ``role`` claim (Supabase sets this to ``"authenticated"`` by default, which
  we map to ``"agent:read"`` so signed-in users get read-level access without
  further configuration).
* ``org_id`` is pulled from ``app_metadata.org_id`` when present.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient

from obscura.auth.models import VALID_ROLES, AuthenticatedUser

logger = logging.getLogger(__name__)

# Tokens minted by Supabase with the default `authenticated` role get this
# baseline set of Obscura roles. Override per-user by writing to
# `app_metadata.roles` in Supabase.
_DEFAULT_AUTHENTICATED_ROLES: tuple[str, ...] = ("agent:read",)


class SupabaseAuthError(Exception):
    """Raised when a Supabase bearer token cannot be validated."""


@dataclass(frozen=True)
class SupabaseSettings:
    """Resolved runtime settings for Supabase JWT verification."""

    jwt_secret: str = ""
    jwks_url: str = ""
    audience: str = "authenticated"
    issuer: str = ""

    @property
    def enabled(self) -> bool:
        """Supabase auth is enabled when we have either a secret or a JWKS URL."""
        return bool(self.jwt_secret) or bool(self.jwks_url)


class SupabaseVerifier:
    """Stateless JWT verifier with a cached JWKS client for RS256 tokens."""

    def __init__(self, settings: SupabaseSettings) -> None:
        self._settings = settings
        self._jwks_client: PyJWKClient | None = None
        self._jwks_lock = threading.Lock()

    # -- public API --------------------------------------------------------

    def verify(self, token: str) -> AuthenticatedUser:
        """Verify *token* and return the authenticated user.

        Raises :class:`SupabaseAuthError` on any failure (bad signature,
        expired, missing claims, unconfigured verifier).
        """
        if not self._settings.enabled:
            raise SupabaseAuthError("Supabase verifier is not configured")

        try:
            claims = self._decode(token)
        except jwt.InvalidTokenError as exc:
            raise SupabaseAuthError(f"invalid token: {exc}") from exc

        return self._user_from_claims(claims, token)

    # -- internals ---------------------------------------------------------

    def _decode(self, token: str) -> dict[str, Any]:
        """Decode and verify *token*, returning the claim set."""
        options = {"require": ["exp", "sub"]}
        issuer = self._settings.issuer or None

        if self._settings.jwks_url:
            signing_key = self._signing_key_for(token)
            return jwt.decode(  # type: ignore[no-any-return]
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self._settings.audience,
                issuer=issuer,
                options=options,
            )

        return jwt.decode(  # type: ignore[no-any-return]
            token,
            self._settings.jwt_secret,
            algorithms=["HS256"],
            audience=self._settings.audience,
            issuer=issuer,
            options=options,
        )

    def _signing_key_for(self, token: str) -> Any:
        """Lazily build a ``PyJWKClient`` and return the key matching the token's kid."""
        with self._jwks_lock:
            client = self._jwks_client
            if client is None:
                client = PyJWKClient(self._settings.jwks_url)
                self._jwks_client = client
        return client.get_signing_key_from_jwt(token).key

    def _user_from_claims(
        self,
        claims: dict[str, Any],
        token: str,
    ) -> AuthenticatedUser:
        """Project verified Supabase claims into an :class:`AuthenticatedUser`."""
        user_id = str(claims.get("sub") or "")
        if not user_id:
            raise SupabaseAuthError("token missing `sub` claim")

        email = str(claims.get("email") or f"{user_id}@supabase.local")
        app_metadata = claims.get("app_metadata") or {}
        if not isinstance(app_metadata, dict):
            app_metadata = {}

        roles = _extract_roles(app_metadata, claims)
        org_id = app_metadata.get("org_id")
        if org_id is not None:
            org_id = str(org_id)

        return AuthenticatedUser(
            user_id=user_id,
            email=email,
            roles=roles,
            org_id=org_id,
            token_type="user",
            raw_token=token,
        )


def _extract_roles(
    app_metadata: dict[str, Any],
    claims: dict[str, Any],
) -> tuple[str, ...]:
    """Pull Obscura roles out of Supabase metadata.

    Precedence: ``app_metadata.roles`` → ``app_metadata.role`` → top-level
    ``role`` claim → default ``("agent:read",)``.
    """
    raw: Any = app_metadata.get("roles")
    if raw is None:
        raw = app_metadata.get("role")
    if raw is None:
        raw = claims.get("role")

    if isinstance(raw, (list, tuple)):
        candidates = tuple(str(r) for r in raw if isinstance(r, str))
    elif isinstance(raw, str):
        # Supabase's default `authenticated` role is not a valid Obscura role.
        candidates = (
            _DEFAULT_AUTHENTICATED_ROLES if raw == "authenticated" else (raw,)
        )
    else:
        candidates = _DEFAULT_AUTHENTICATED_ROLES

    # Filter to known Obscura roles so a compromised/misconfigured token
    # can't grant arbitrary scope by stuffing `app_metadata.roles`.
    filtered = tuple(r for r in candidates if r in VALID_ROLES)
    return filtered or _DEFAULT_AUTHENTICATED_ROLES


# ---------------------------------------------------------------------------
# Module-level singleton bound to ObscuraConfig
# ---------------------------------------------------------------------------


_verifier: SupabaseVerifier | None = None
_verifier_lock = threading.Lock()
_verifier_settings_key: tuple[str, str, str, str] | None = None


def get_verifier(
    jwt_secret: str,
    jwks_url: str,
    audience: str,
    issuer: str,
) -> SupabaseVerifier:
    """Return a process-wide verifier, rebuilt when settings change."""
    global _verifier, _verifier_settings_key

    key = (jwt_secret, jwks_url, audience, issuer)
    with _verifier_lock:
        if _verifier is None or _verifier_settings_key != key:
            _verifier = SupabaseVerifier(
                SupabaseSettings(
                    jwt_secret=jwt_secret,
                    jwks_url=jwks_url,
                    audience=audience,
                    issuer=issuer,
                ),
            )
            _verifier_settings_key = key
        return _verifier


def reset_verifier_for_tests() -> None:
    """Test helper — drop the cached verifier."""
    global _verifier, _verifier_settings_key
    with _verifier_lock:
        _verifier = None
        _verifier_settings_key = None


__all__ = [
    "SupabaseAuthError",
    "SupabaseSettings",
    "SupabaseVerifier",
    "get_verifier",
    "reset_verifier_for_tests",
]
