"""
Routes: capability tier introspection and token management.

Provides endpoints for resolving the caller's capability tier,
generating capability tokens, and validating tokens (admin diagnostic).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from sdk.auth.capability import (
    CapabilityTier,
    CapabilityToken,
    generate_capability_token,
    resolve_tier,
    validate_capability_token,
)
from sdk.auth.models import AuthenticatedUser
from sdk.auth.rbac import get_current_user, require_role
from sdk.deps import audit

router = APIRouter(prefix="/api/v1", tags=["capabilities"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class TokenRequest(BaseModel):
    """Request body for POST /api/v1/capabilities/token."""

    session_id: str = Field(..., min_length=1, description="Session identifier")


class TokenValidateRequest(BaseModel):
    """Request body for POST /api/v1/capabilities/validate."""

    tier: str
    user_id: str
    session_id: str
    issued_at: float
    expires_at: float
    nonce: str
    signature: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/capabilities/tier")
async def get_tier(
    user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    """Return the caller's resolved capability tier."""
    tier = resolve_tier(user)
    audit(
        "capability.resolve",
        user,
        f"tier:{tier.value}",
        "read",
        "success",
        resolved_tier=tier.value,
    )
    return JSONResponse(
        content={
            "tier": tier.value,
            "user_id": user.user_id,
            "roles": list(user.roles),
        }
    )


@router.post("/capabilities/token")
async def create_capability_token(
    body: TokenRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    """Generate a capability token for the current session."""
    token = generate_capability_token(user, body.session_id)

    audit(
        "capability.token.generate",
        user,
        f"session:{body.session_id}",
        "create",
        "success",
        tier=token.tier.value,
        expires_at=token.expires_at,
    )

    return JSONResponse(
        content={
            "tier": token.tier.value,
            "session_id": body.session_id,
            "expires_at": token.expires_at,
            "token": token.to_dict(),
        }
    )


@router.post("/capabilities/validate")
async def validate_token(
    body: TokenValidateRequest,
    user: AuthenticatedUser = Depends(require_role("admin")),
) -> JSONResponse:
    """Validate a capability token (admin diagnostic endpoint)."""
    try:
        token = CapabilityToken(
            tier=CapabilityTier(body.tier),
            user_id=body.user_id,
            session_id=body.session_id,
            issued_at=body.issued_at,
            expires_at=body.expires_at,
            nonce=body.nonce,
            signature=body.signature,
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid token format: {e}")

    valid = validate_capability_token(token)

    audit(
        "capability.token.validate",
        user,
        f"token:{body.user_id}",
        "validate",
        "success" if valid else "denied",
        valid=valid,
        expired=token.is_expired(),
    )

    return JSONResponse(
        content={
            "valid": valid,
            "tier": token.tier.value,
            "expired": token.is_expired(),
        }
    )
