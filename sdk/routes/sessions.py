"""Routes: session management."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from sdk.internal.types import Backend, SessionRef
from sdk.auth.models import AuthenticatedUser
from sdk.auth.rbac import require_role
from sdk.deps import ClientFactory, audit
from sdk.schemas import SessionCreateRequest, SessionResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["sessions"])


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
        return SessionResponse(session_id=ref.session_id, backend=ref.backend.value)
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
                        )
                    )
            finally:
                await client.stop()
        except Exception:
            logger.debug("Could not list sessions for %s", backend_name, exc_info=True)
    return results


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
        return JSONResponse(content={"deleted": True, "session_id": session_id})
    finally:
        await client.stop()
