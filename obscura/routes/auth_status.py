"""Routes: ingress/egress authentication diagnostics."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from obscura.auth.models import AuthenticatedUser
from obscura.auth.rbac import AGENT_READ_ROLES, require_any_role
from obscura.core.auth import AuthConfig, resolve_auth
from obscura.core.types import Backend

router = APIRouter(prefix="/api/v1", tags=["auth"])


def _provider_status(backend: Backend) -> dict[str, Any]:
    try:
        resolved: AuthConfig = resolve_auth(backend)
        return {
            "ok": True,
            "backend": backend.value,
            "mode": "oauth" if backend == Backend.CLAUDE and resolved.anthropic_api_key is None else "resolved",
            "reason": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "backend": backend.value,
            "mode": "missing",
            "reason": str(exc),
        }


@router.get("/auth/diagnostics")
async def auth_diagnostics(
    request: Request,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Report ingress auth mode and server-side auth configuration."""
    config = request.app.state.config
    payload = {
        "auth_enabled": bool(config.auth_enabled),
        "auth_mode": "api_key",
        "request_auth_type": user.token_type,
        "request_user_id": user.user_id,
        "has_api_key_header": bool(request.headers.get("X-API-Key")),
    }
    return JSONResponse(content=payload)


@router.get("/providers/health")
async def providers_health(
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
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
        }
    )
