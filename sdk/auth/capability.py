"""
sdk.auth.capability -- Cryptographic capability tokens for tiered LLM access.

Generates and validates HMAC-SHA256 signed capability tokens that bind
a user's identity and resolved tier to a non-forgeable opaque token.
The orchestrator attaches these tokens server-side; models never see the
signing key and cannot fabricate valid tokens.

Tiers
-----
- ``PUBLIC``      -- Tier A: minimal tools, strict prompt filters, no debug
- ``PRIVILEGED``  -- Tier B: full tools, debug access, relaxed filters

Usage::

    from sdk.auth.capability import resolve_tier, generate_capability_token

    tier = resolve_tier(user)                     # from JWT roles
    token = generate_capability_token(user, sid)  # HMAC-signed
    assert validate_capability_token(token)       # recomputes HMAC
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from sdk.auth.models import AuthenticatedUser


# ---------------------------------------------------------------------------
# Tier enumeration
# ---------------------------------------------------------------------------


class CapabilityTier(enum.Enum):
    """The two tiers of capability access."""

    PUBLIC = "public"  # Tier A: minimal tools, strict filtering
    PRIVILEGED = "privileged"  # Tier B: full tools, debug, bypass safety


# ---------------------------------------------------------------------------
# Roles that grant Tier B
# ---------------------------------------------------------------------------

PRIVILEGED_ROLES: frozenset[str] = frozenset(
    {
        "admin",
        "operator",
        "tier:privileged",
    }
)


# ---------------------------------------------------------------------------
# Capability token
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityToken:
    """A cryptographically signed capability token.

    Generated server-side, validated server-side.  The model never
    sees the signing key and cannot forge a valid token.
    """

    tier: CapabilityTier
    user_id: str
    session_id: str
    issued_at: float
    expires_at: float
    nonce: str
    signature: str

    def is_expired(self) -> bool:
        """Return True if the token has exceeded its TTL."""
        return time.time() > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Serialise for API responses (never sent to the model)."""
        return {
            "tier": self.tier.value,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "signature": self.signature,
        }


# ---------------------------------------------------------------------------
# Tier resolution
# ---------------------------------------------------------------------------


def resolve_tier(user: AuthenticatedUser) -> CapabilityTier:
    """Determine the capability tier from the user's JWT roles.

    Tier B (PRIVILEGED) requires the user to hold one of the roles in
    :data:`PRIVILEGED_ROLES`.  This is the **only** path to privilege
    escalation -- no prompt content, no magic words.
    """
    if set(user.roles) & PRIVILEGED_ROLES:
        return CapabilityTier.PRIVILEGED
    return CapabilityTier.PUBLIC


# ---------------------------------------------------------------------------
# Signing key management
# ---------------------------------------------------------------------------

_SIGNING_KEY: bytes | None = None


def _get_signing_key() -> bytes:
    """Return the HMAC signing key, loading from env on first call.

    In production set ``OBSCURA_CAPABILITY_SECRET``.  If unset a random
    32-byte key is generated per-process (fine for dev, not for clusters).
    """
    global _SIGNING_KEY
    if _SIGNING_KEY is None:
        env_key = os.environ.get("OBSCURA_CAPABILITY_SECRET")
        if env_key:
            _SIGNING_KEY = env_key.encode("utf-8")
        else:
            _SIGNING_KEY = secrets.token_bytes(32)
    return _SIGNING_KEY


def _reset_signing_key() -> None:
    """Reset the cached signing key (for testing only)."""
    global _SIGNING_KEY
    _SIGNING_KEY = None


# ---------------------------------------------------------------------------
# Token generation and validation
# ---------------------------------------------------------------------------


def _compute_signature(payload: dict[str, Any]) -> str:
    """Compute HMAC-SHA256 over the canonical JSON representation."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        _get_signing_key(),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def generate_capability_token(
    user: AuthenticatedUser,
    session_id: str,
    *,
    ttl_seconds: int = 3600,
    tier_override: CapabilityTier | None = None,
) -> CapabilityToken:
    """Generate a signed capability token for *user* + *session_id*.

    Parameters
    ----------
    user:
        The authenticated user (from JWT / API key).
    session_id:
        The current agent or API session identifier.
    ttl_seconds:
        How long the token is valid (default 1 hour).
    tier_override:
        Force a specific tier (e.g. for admin testing).

    Returns
    -------
    CapabilityToken
        A frozen, HMAC-signed token.
    """
    tier = tier_override if tier_override is not None else resolve_tier(user)
    now = time.time()
    nonce = secrets.token_hex(16)

    payload = {
        "tier": tier.value,
        "user_id": user.user_id,
        "session_id": session_id,
        "issued_at": now,
        "expires_at": now + ttl_seconds,
        "nonce": nonce,
    }
    signature = _compute_signature(payload)

    return CapabilityToken(
        tier=tier,
        user_id=user.user_id,
        session_id=session_id,
        issued_at=now,
        expires_at=now + ttl_seconds,
        nonce=nonce,
        signature=signature,
    )


def validate_capability_token(token: CapabilityToken) -> bool:
    """Validate a capability token's HMAC signature and expiry.

    Recomputes the HMAC over the same payload fields and performs a
    constant-time comparison.  Returns ``False`` for expired or
    tampered tokens.
    """
    if token.is_expired():
        return False

    payload = {
        "tier": token.tier.value,
        "user_id": token.user_id,
        "session_id": token.session_id,
        "issued_at": token.issued_at,
        "expires_at": token.expires_at,
        "nonce": token.nonce,
    }
    expected_sig = _compute_signature(payload)
    return hmac.compare_digest(token.signature, expected_sig)
