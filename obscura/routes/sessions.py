"""Routes: session management."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from obscura.auth.models import AuthenticatedUser
from obscura.auth.rbac import require_role
from obscura.core.event_store import SQLiteEventStore
from obscura.core.types import Backend, SessionRef
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


def _get_event_store(request: Request) -> SQLiteEventStore:
    """Get the shared event store from app state."""
    store: SQLiteEventStore | None = getattr(request.app.state, "event_store", None)
    if store is None:
        from obscura.core.paths import resolve_obscura_home

        store = SQLiteEventStore(resolve_obscura_home() / "events.db")
        request.app.state.event_store = store
    return store


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
    backend: str | None = None,
    source: str | None = None,
) -> list[SessionResponse]:
    """List all sessions from the unified event store."""
    store = _get_event_store(request)
    records = await store.list_sessions(backend=backend, source=source)
    return [
        SessionResponse(
            session_id=rec.id,
            backend=rec.backend,
            created_at=rec.created_at.isoformat() if rec.created_at else None,
            source=rec.source,
        )
        for rec in records
    ]


@router.post("/sessions/ingest")
async def ingest_sessions(
    body: dict[str, Any] | None = None,
    request: Request = None,  # type: ignore[assignment]
    user: AuthenticatedUser = Depends(require_role("sessions:manage")),
) -> JSONResponse:
    """Ingest system sessions from ~/.obscura into the unified event store."""
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

    # Pass the shared event store
    store = _get_event_store(request) if request else None

    try:
        result = await asyncio.to_thread(
            sync_and_ingest_system_sessions,
            user,
            agent=agent,
            force=force,
            copy_to_pwd=copy_to_pwd,
            copy_overwrite=copy_overwrite,
            store=store,
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
