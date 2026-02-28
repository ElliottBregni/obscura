"""Routes: session management."""

from __future__ import annotations

import logging
import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from obscura.core.types import Backend, SessionRef
from obscura.auth.models import AuthenticatedUser
from obscura.auth.rbac import require_role
from obscura.deps import ClientFactory, audit
from obscura.routes.session_ingest import (
    preflight_system_session_ingest,
    sync_and_ingest_system_sessions,
)
from obscura.routes.session_sync import sync_session_lifecycle
from obscura.routes.websockets import broadcast_event
from obscura.schemas import SessionCreateRequest, SessionResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["sessions"])


def _append_ingested_sessions(
    results: list[SessionResponse],
    user: AuthenticatedUser,
) -> list[SessionResponse]:
    """Merge memory-ingested system sessions into the live session list."""
    from obscura.memory import MemoryStore

    seen = {(r.backend, r.session_id) for r in results}
    store = MemoryStore.for_user(user)
    for mem_key in store.list_keys(namespace="sessions"):
        raw = store.get(mem_key.key, namespace="sessions")
        if not isinstance(raw, dict):
            continue
        payload = raw
        backend = str(payload.get("agent") or payload.get("backend") or "").strip()
        session_id = str(payload.get("id") or mem_key.key).strip()
        if not backend or not session_id:
            continue
        dedupe_key = (backend, session_id)
        if dedupe_key in seen:
            continue
        results.append(
            SessionResponse(
                session_id=session_id,
                backend=backend,
                created_at=(
                    str(payload.get("started") or payload.get("created_at"))
                    if (payload.get("started") or payload.get("created_at"))
                    else None
                ),
                source="ingested",
            )
        )
        seen.add(dedupe_key)
    return results


@router.post("/sessions", response_model=SessionResponse)
async def create_session(
    body: SessionCreateRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_role("sessions:manage")),
) -> SessionResponse:
    """Create a new session."""
    factory: ClientFactory = request.app.state.client_factory
    client = await factory.create(body.backend, user=user)
    try:
        ref = await client.create_session()
        audit(
            "session.create",
            user,
            f"backend:{body.backend}",
            "write",
            "success",
            session_id=ref.session_id,
        )
        try:
            sync_session_lifecycle(
                user=user,
                session_id=ref.session_id,
                backend=ref.backend.value,
                event="created",
            )
        except Exception:
            logger.debug("Failed to sync session create into vector memory", exc_info=True)
        asyncio.create_task(
            broadcast_event(
                "session_created",
                {"session_id": ref.session_id, "backend": ref.backend.value},
            )
        )
        return SessionResponse(
            session_id=ref.session_id,
            backend=ref.backend.value,
            source="live",
        )
    finally:
        await client.stop()


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    request: Request,
    user: AuthenticatedUser = Depends(require_role("sessions:manage")),
) -> list[SessionResponse]:
    """List available sessions across all backends."""
    results: list[SessionResponse] = []
    for backend_name in ("copilot", "claude"):
        factory: ClientFactory = request.app.state.client_factory
        try:
            client = await factory.create(backend_name, user=user)
            try:
                refs = await client.list_sessions()
                for ref in refs:
                    results.append(
                        SessionResponse(
                            session_id=ref.session_id,
                            backend=ref.backend.value,
                            source="live",
                        )
                    )
            finally:
                await client.stop()
        except ValueError:
            # Missing provider credentials is expected in many dev setups.
            # Surface this via /api/v1/providers/health instead of log noise.
            continue
        except Exception:
            logger.debug("Could not list sessions for %s", backend_name, exc_info=True)
    return _append_ingested_sessions(results, user)


@router.post("/sessions/ingest")
async def ingest_sessions(
    body: dict[str, Any] | None = None,
    user: AuthenticatedUser = Depends(require_role("sessions:manage")),
) -> JSONResponse:
    """Ingest system sessions from ~/.obscura into memory + vector memory."""
    payload = body or {}
    agent_raw = payload.get("agent")
    agent = str(agent_raw).strip() if agent_raw else None
    if agent and agent not in {"codex", "claude", "copilot"}:
        return JSONResponse(
            status_code=400,
            content={"error": "agent must be one of codex, claude, copilot"},
        )
    force = bool(payload.get("force", False))
    copy_to_pwd = bool(payload.get("copy_to_pwd", False))
    copy_overwrite = bool(payload.get("copy_overwrite", True))

    try:
        result = await asyncio.to_thread(
            sync_and_ingest_system_sessions,
            user,
            agent=agent,
            force=force,
            copy_to_pwd=copy_to_pwd,
            copy_overwrite=copy_overwrite,
        )
    except Exception as exc:
        audit(
            "session.ingest",
            user,
            "sessions:system",
            "execute",
            "error",
            reason=str(exc),
            agent=agent,
            force=force,
            copy_to_pwd=copy_to_pwd,
        )
        return JSONResponse(status_code=500, content={"success": False, "error": str(exc)})

    audit(
        "session.ingest",
        user,
        "sessions:system",
        "execute",
        "success",
        agent=agent,
        force=force,
        ingested=result.get("ingested", 0),
        skipped=result.get("skipped", 0),
        entries=result.get("entries", 0),
        copy_to_pwd=copy_to_pwd,
    )
    asyncio.create_task(
        broadcast_event(
            "session_ingested",
            {
                "agent": agent or "all",
                "ingested": result.get("ingested", 0),
                "skipped": result.get("skipped", 0),
                "entries": result.get("entries", 0),
                "copy_to_pwd": copy_to_pwd,
            },
        )
    )
    return JSONResponse(content={"success": True, **result})


@router.get("/sessions/ingest/preflight")
async def ingest_sessions_preflight(
    user: AuthenticatedUser = Depends(require_role("sessions:manage")),
) -> JSONResponse:
    """Return health checks for system session ingest readiness."""
    checks = preflight_system_session_ingest()
    audit(
        "session.ingest.preflight",
        user,
        "sessions:system",
        "read",
        "success",
        ready=checks.get("ready", False),
    )
    return JSONResponse(content=checks)


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    request: Request,
    backend: str = "copilot",
    user: AuthenticatedUser = Depends(require_role("sessions:manage")),
) -> JSONResponse:
    """Delete a session by ID."""
    factory: ClientFactory = request.app.state.client_factory
    client = await factory.create(backend, user=user)
    try:
        ref = SessionRef(session_id=session_id, backend=Backend(backend))
        await client.delete_session(ref)
        audit(
            "session.delete",
            user,
            f"session:{session_id}",
            "delete",
            "success",
            backend=backend,
        )
        try:
            sync_session_lifecycle(
                user=user,
                session_id=session_id,
                backend=backend,
                event="deleted",
            )
        except Exception:
            logger.debug("Failed to sync session delete into vector memory", exc_info=True)
        asyncio.create_task(
            broadcast_event(
                "session_deleted",
                {"session_id": session_id, "backend": backend},
            )
        )
        return JSONResponse(content={"deleted": True, "session_id": session_id})
    finally:
        await client.stop()
