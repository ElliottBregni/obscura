"""Routes: send / stream prompts."""

from __future__ import annotations

from typing import AsyncGenerator

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from sdk.internal.types import (
    Backend,
    ExecutionMode,
    ProviderNativeRequest,
    SessionRef,
    UnifiedRequest,
)
from sdk.auth.models import AuthenticatedUser
from sdk.auth.rbac import AGENT_READ_ROLES, require_any_role
from sdk.deps import ClientFactory, audit
from sdk.schemas import SendRequest, SendResponse, StreamRequest

router = APIRouter(prefix="/api/v1", tags=["agent"])


@router.post("/send", response_model=SendResponse)
async def send(
    body: SendRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
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

        mode = (
            ExecutionMode(body.mode)
            if body.mode in ("unified", "native")
            else ExecutionMode.UNIFIED
        )
        native_payload: ProviderNativeRequest | None = None
        if body.native is not None:
            native_payload = ProviderNativeRequest(
                openai=body.native.get("openai")
                if isinstance(body.native.get("openai"), dict)
                else None,
                claude=body.native.get("claude")
                if isinstance(body.native.get("claude"), dict)
                else None,
                copilot=body.native.get("copilot")
                if isinstance(body.native.get("copilot"), dict)
                else None,
                localllm=body.native.get("localllm")
                if isinstance(body.native.get("localllm"), dict)
                else None,
            )
        unified_req = UnifiedRequest(
            prompt=body.prompt,
            mode=mode,
            native=native_payload,
        )

        msg = await client.send(
            body.prompt,
            mode=body.mode,
            api_mode=body.api_mode,
            native=body.native,
            request=unified_req,
        )
        audit(
            "agent.send",
            user,
            f"backend:{body.backend}",
            "execute",
            "success",
            prompt_len=len(body.prompt),
        )
        return SendResponse(
            text=msg.text,
            backend=body.backend,
            capability_tier=client.capability_tier,
        )
    except Exception:
        audit(
            "agent.send",
            user,
            f"backend:{body.backend}",
            "execute",
            "error",
            prompt_len=len(body.prompt),
        )
        raise
    finally:
        await client.stop()


@router.post("/stream")
async def stream(
    body: StreamRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
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
                ref = SessionRef(
                    session_id=body.session_id, backend=Backend(body.backend)
                )
                await client.resume_session(ref)

            mode = (
                ExecutionMode(body.mode)
                if body.mode in ("unified", "native")
                else ExecutionMode.UNIFIED
            )
            native_payload: ProviderNativeRequest | None = None
            if body.native is not None:
                native_payload = ProviderNativeRequest(
                    openai=body.native.get("openai")
                    if isinstance(body.native.get("openai"), dict)
                    else None,
                    claude=body.native.get("claude")
                    if isinstance(body.native.get("claude"), dict)
                    else None,
                    copilot=body.native.get("copilot")
                    if isinstance(body.native.get("copilot"), dict)
                    else None,
                    localllm=body.native.get("localllm")
                    if isinstance(body.native.get("localllm"), dict)
                    else None,
                )
            unified_req = UnifiedRequest(
                prompt=body.prompt,
                mode=mode,
                native=native_payload,
            )

            async for chunk in client.stream(
                body.prompt,
                mode=body.mode,
                api_mode=body.api_mode,
                native=body.native,
                request=unified_req,
            ):
                yield {
                    "event": chunk.kind.value,
                    "data": chunk.text or chunk.tool_name or "",
                }
        finally:
            await client.stop()

    return EventSourceResponse(_event_generator())
