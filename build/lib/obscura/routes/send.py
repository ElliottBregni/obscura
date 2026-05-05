"""Routes: send / stream prompts."""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from obscura.auth.rbac import AGENT_READ_ROLES, require_any_role
from obscura.core.enums.agent import Backend, ExecutionMode
from obscura.core.types import (
    ProviderNativeRequest,
    SessionRef,
    UnifiedRequest,
)
from obscura.deps import ClientFactory, audit, get_oauth_github_token
from obscura.routes.session_sync import sync_session_turn
from obscura.schemas import SendRequest, SendResponse, StreamRequest

from obscura.auth.models import AuthenticatedUser

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

router = APIRouter(prefix="/api/v1", tags=["agent"])


@router.post("/send", response_model=SendResponse)
async def send(
    body: SendRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
    oauth_gh_token: Annotated[str | None, Depends(get_oauth_github_token)] = None,
) -> SendResponse:
    """Send a prompt and receive the full response."""
    factory: ClientFactory = request.app.state.client_factory
    client = await factory.create(
        body.backend,
        user=user,
        model=body.model,
        model_alias=body.model_alias,
        system_prompt=body.system_prompt,
        oauth_github_token=oauth_gh_token,
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
                codex=body.native.get("codex")
                if isinstance(body.native.get("codex"), dict)
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
        if body.session_id:
            with contextlib.suppress(Exception):
                sync_session_turn(
                    user=user,
                    session_id=body.session_id,
                    backend=body.backend,
                    prompt=body.prompt,
                    response=msg.text,
                    mode=body.mode,
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


@router.post("/messages", response_model=SendResponse)
async def messages_send(
    body: SendRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
    oauth_gh_token: Annotated[str | None, Depends(get_oauth_github_token)] = None,
) -> SendResponse:
    """Phase-1 alias for send; keeps CLI/API surface aligned."""
    return await send(body, request, user, oauth_gh_token)


@router.post("/messages/stream")
async def messages_stream(
    body: StreamRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
    oauth_gh_token: Annotated[str | None, Depends(get_oauth_github_token)] = None,
) -> EventSourceResponse:
    """Phase-1 alias for stream; keeps CLI/API surface aligned."""
    return await stream(body, request, user, oauth_gh_token)


@router.post("/stream")
async def stream(
    body: StreamRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
    oauth_gh_token: Annotated[str | None, Depends(get_oauth_github_token)] = None,
) -> EventSourceResponse:
    """Send a prompt and receive an SSE event stream."""

    async def _event_generator() -> AsyncGenerator[dict[str, str]]:
        factory: ClientFactory = request.app.state.client_factory
        client = await factory.create(
            body.backend,
            user=user,
            model=body.model,
            model_alias=body.model_alias,
            system_prompt=body.system_prompt,
            oauth_github_token=oauth_gh_token,
        )
        response_text_parts: list[str] = []
        try:
            if body.session_id:
                ref = SessionRef(
                    session_id=body.session_id,
                    backend=Backend(body.backend),
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
                    codex=body.native.get("codex")
                    if isinstance(body.native.get("codex"), dict)
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
                payload: dict[str, str] = {}
                if chunk.text:
                    payload["text"] = chunk.text
                    if chunk.kind.value == "text_delta":
                        response_text_parts.append(chunk.text)
                if chunk.tool_name:
                    payload["tool_name"] = chunk.tool_name
                if chunk.tool_input_delta:
                    payload["tool_input_delta"] = chunk.tool_input_delta
                if chunk.tool_use_id:
                    payload["tool_use_id"] = chunk.tool_use_id
                if chunk.metadata:
                    payload["metadata"] = json.dumps(
                        {
                            "finish_reason": chunk.metadata.finish_reason,
                            "model_id": chunk.metadata.model_id,
                            "usage": chunk.metadata.usage,
                        },
                    )
                yield {
                    "event": chunk.kind.value,
                    "data": json.dumps(payload),
                }
            if body.session_id:
                with contextlib.suppress(Exception):
                    sync_session_turn(
                        user=user,
                        session_id=body.session_id,
                        backend=body.backend,
                        prompt=body.prompt,
                        response="".join(response_text_parts),
                        mode=body.mode,
                    )
        finally:
            await client.stop()

    return EventSourceResponse(_event_generator())
