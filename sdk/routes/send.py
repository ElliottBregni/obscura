"""Routes: send / stream prompts."""

from __future__ import annotations

from typing import AsyncGenerator

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from sdk._types import Backend, SessionRef
from sdk.auth.models import AuthenticatedUser
from sdk.auth.rbac import require_any_role
from sdk.deps import ClientFactory, audit
from sdk.schemas import SendRequest, SendResponse, StreamRequest

router = APIRouter(prefix="/api/v1", tags=["agent"])


@router.post("/send", response_model=SendResponse)
async def send(
    body: SendRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
) -> SendResponse:
    """Send a prompt and receive the full response."""
    factory: ClientFactory = request.app.state.client_factory
    client = await factory.create(
        body.backend,
        user=user,
        model=body.model,
        model_alias=body.model_alias,
        system_prompt=body.system_prompt,
    )
    try:
        if body.session_id:
            ref = SessionRef(session_id=body.session_id, backend=Backend(body.backend))
            await client.resume_session(ref)

        msg = await client.send(body.prompt)
        audit("agent.send", user, f"backend:{body.backend}", "execute", "success",
              prompt_len=len(body.prompt))
        return SendResponse(text=msg.text, backend=body.backend)
    except Exception:
        audit("agent.send", user, f"backend:{body.backend}", "execute", "error",
              prompt_len=len(body.prompt))
        raise
    finally:
        await client.stop()


@router.post("/stream")
async def stream(
    body: StreamRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
) -> EventSourceResponse:
    """Send a prompt and receive an SSE event stream."""

    async def _event_generator() -> AsyncGenerator[dict[str, str], None]:
        factory: ClientFactory = request.app.state.client_factory
        client = await factory.create(
            body.backend,
            user=user,
            model=body.model,
            model_alias=body.model_alias,
            system_prompt=body.system_prompt,
        )
        try:
            if body.session_id:
                ref = SessionRef(session_id=body.session_id, backend=Backend(body.backend))
                await client.resume_session(ref)

            async for chunk in client.stream(body.prompt):
                yield {
                    "event": chunk.kind.value,
                    "data": chunk.text or chunk.tool_name or "",
                }
        finally:
            await client.stop()

    return EventSourceResponse(_event_generator())
