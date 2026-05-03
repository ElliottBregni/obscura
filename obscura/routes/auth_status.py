"""Routes: ingress/egress authentication diagnostics."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from obscura.auth.cli_session import (
    StoredSession,
    SupabaseCliConfig,
    clear_session,
    get_access_token,
    get_github_token,
    load_session,
    sync_provider_secrets_to_supabase,
)
from obscura.auth.models import AuthenticatedUser
from obscura.auth.rbac import AGENT_READ_ROLES, require_any_role
from obscura.core.auth import AuthConfig, resolve_auth
from obscura.core.types import Backend
import logging

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/v1", tags=["auth"])


def _provider_status(backend: Backend) -> dict[str, Any]:
    try:
        resolved: AuthConfig = resolve_auth(backend)
        return {
            "ok": True,
            "backend": backend.value,
            "mode": "oauth"
            if backend == Backend.CLAUDE and resolved.anthropic_api_key is None
            else "resolved",
            "reason": "",
        }
    except Exception as exc:
        logger.debug("suppressed exception in _provider_status", exc_info=True)
        return {
            "ok": False,
            "backend": backend.value,
            "mode": "missing",
            "reason": str(exc),
        }


@router.get("/auth/diagnostics")
async def auth_diagnostics(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
) -> JSONResponse:
    """Report ingress auth mode and server-side auth configuration."""
    payload = {
        "auth_mode": "api_key",
        "request_auth_type": user.token_type,
        "request_user_id": user.user_id,
        "has_api_key_header": bool(request.headers.get("X-API-Key")),
    }
    return JSONResponse(content=payload)


@router.get("/providers/health")
async def providers_health(
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
) -> JSONResponse:
    """Report provider egress auth readiness."""
    statuses = [
        _provider_status(Backend.COPILOT),
        _provider_status(Backend.CLAUDE),
        _provider_status(Backend.OPENAI),
        _provider_status(Backend.CODEX),
        _provider_status(Backend.MOONSHOT),
        _provider_status(Backend.LOCALLLM),
    ]
    ok = all(s["ok"] for s in statuses if s["backend"] != Backend.MOONSHOT.value)
    return JSONResponse(
        content={
            "ok": ok,
            "providers": statuses,
        },
    )


class ProviderSecretsSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["github", "google"]
    provider_token: str | None = None
    provider_refresh_token: str | None = None


class AuthLogoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str | None = None


@router.post("/auth/provider-secrets/sync")
async def sync_provider_secrets(
    body: ProviderSecretsSyncRequest,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
) -> JSONResponse:
    """Sync provider tokens into Supabase user_metadata using shared CLI logic."""
    cfg = SupabaseCliConfig.from_env()
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured. Set SUPABASE_URL and SUPABASE_ANON_KEY.",
        )

    session = StoredSession(
        access_token="",
        refresh_token="",
        expires_at=0,
        user_id=user.user_id,
        email=user.email,
        provider=body.provider,
        provider_token=body.provider_token,
        provider_refresh_token=body.provider_refresh_token,
    )
    _sync_provider_secrets_to_supabase(cfg, provider=body.provider, session=session)

    return JSONResponse(
        content={
            "ok": True,
            "provider": body.provider,
            "user_id": user.user_id,
        },
    )


@router.get("/auth/session")
async def auth_session(
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
) -> JSONResponse:
    """Return current persisted auth session details for API clients."""
    session = load_session()
    token = get_access_token()
    return JSONResponse(
        content={
            "ok": True,
            "authenticated": bool(token),
            "user_id": user.user_id,
            "email": user.email,
            "provider": session.provider if session else None,
            "github_oauth": bool(get_github_token()),
            "expires_at": session.expires_at if session else None,
        },
    )


@router.post("/auth/logout")
async def auth_logout(
    body: AuthLogoutRequest,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
) -> JSONResponse:
    """Clear locally persisted auth credentials."""
    removed = clear_session()
    return JSONResponse(
        content={
            "ok": True,
            "removed": removed,
            "provider": body.provider,
            "user_id": user.user_id,
        },
    )
