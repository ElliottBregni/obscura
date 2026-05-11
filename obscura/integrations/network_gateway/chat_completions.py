"""obscura.integrations.network_gateway.chat_completions — POST /v1/chat/completions.

Implements a minimal OpenAI-compatible chat completions endpoint backed by
an Obscura ``AgentSession``.

Model mapping
-------------
The ``model`` field in the request body selects the Obscura backend:

* ``"obscura"`` or ``""`` — use the gateway's default backend.
* ``"obscura/claude"``    — Claude backend.
* ``"obscura/copilot"``   — GitHub Copilot backend.
* ``"obscura/codex"``     — Codex backend.
* ``"obscura/localllm"``  — Local LLM backend.

Streaming
---------
When ``stream: true`` the handler returns an SSE response where each chunk
is a ``data: <JSON>\\n\\n`` line in the standard ``ChatCompletionChunk``
shape.  The final chunk has ``finish_reason: "stop"`` and is followed by
``data: [DONE]\\n\\n``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models (OpenAI subset)
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"] = "user"
    content: str | None = None

    @field_validator("content", mode="before")
    @classmethod
    def _flatten_multimodal(cls, v: Any) -> Any:
        # OpenAI multimodal form: content can be a list of blocks like
        # [{"type": "text", "text": "..."}]. Flatten to a plain string so the
        # rest of the handler — which treats content as text — keeps working.
        if isinstance(v, list):
            return "".join(
                b.get("text", "")
                for b in v
                if isinstance(b, dict) and b.get("type") == "text"
            )
        return v


class ChatCompletionRequest(BaseModel):
    model: str = "obscura"
    messages: list[ChatMessage]
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None


class ChoiceDelta(BaseModel):
    role: str | None = None
    content: str | None = None


class Choice(BaseModel):
    index: int = 0
    message: ChatMessage | None = None
    delta: ChoiceDelta | None = None
    finish_reason: str | None = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage = Field(default_factory=Usage)


class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[Choice]


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

_MODEL_TO_BACKEND: dict[str, str] = {
    "obscura": "",  # use gateway default
    "obscura/claude": "claude",
    "obscura/copilot": "copilot",
    "obscura/codex": "codex",
    "obscura/localllm": "localllm",
}

_ALLOWED_MODELS: frozenset[str] = frozenset(_MODEL_TO_BACKEND.keys())
_MAX_PROMPT_BYTES: int = 131_072  # 128 KB


def _resolve_backend(model: str, default_backend: str) -> str:
    """Map model string to an Obscura backend identifier."""
    if not model or model not in _MODEL_TO_BACKEND:
        return default_backend
    backend = _MODEL_TO_BACKEND[model]
    return backend or default_backend


# ---------------------------------------------------------------------------
# Agent execution helpers
# ---------------------------------------------------------------------------


def _extract_prompt(messages: list[ChatMessage]) -> tuple[str, str]:
    """Split messages into (system_prompt, last_user_prompt).

    The system prompt is the concatenation of all ``system`` role messages.
    The user prompt is the last ``user`` role message.
    """
    system_parts: list[str] = []
    user_prompt = ""
    for msg in messages:
        if msg.role == "system" and msg.content:
            system_parts.append(msg.content)
        elif msg.role == "user" and msg.content:
            user_prompt = msg.content

    return "\n\n".join(system_parts), user_prompt


async def _run_agent(
    backend: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> str:
    """Run one agent turn and return the full text response."""
    from obscura.composition.a2a import build_a2a_session
    from obscura.composition.session import SessionConfig

    config = SessionConfig(
        backend=backend,
        model=model or None,
        system_prompt=system_prompt,
        max_turns=10,
    )

    async with await build_a2a_session(
        config,
        task_id=f"gw-{uuid.uuid4().hex[:12]}",
    ) as session:
        result = await session.run_loop_to_text(user_prompt)
        return result or ""


async def _stream_agent(
    backend: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> AsyncIterator[str]:
    """Stream agent events as text deltas."""
    from obscura.composition.a2a import build_a2a_session
    from obscura.composition.session import SessionConfig
    from obscura.core.enums.agent import AgentEventKind

    config = SessionConfig(
        backend=backend,
        model=model or None,
        system_prompt=system_prompt,
        max_turns=10,
    )

    async with await build_a2a_session(
        config,
        task_id=f"gw-{uuid.uuid4().hex[:12]}",
    ) as session:
        async for event in session.stream_loop(user_prompt):
            if event.kind == AgentEventKind.TEXT_DELTA:
                text: str = getattr(event, "text", "") or ""
                if text:
                    yield text


# ---------------------------------------------------------------------------
# SSE streaming helper
# ---------------------------------------------------------------------------


async def _sse_generator(
    backend: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    completion_id: str,
    created: int,
) -> AsyncIterator[str]:
    """Yield SSE-formatted lines for a streaming chat completion."""
    # Opening chunk with role
    opening: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
        ],
    }
    yield f"data: {json.dumps(opening)}\n\n"

    try:
        async for delta_text in _stream_agent(
            backend, model, system_prompt, user_prompt
        ):
            chunk: dict[str, Any] = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": delta_text},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
    except Exception:
        logger.exception("Error during streamed agent run")

    # Final chunk
    final: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/v1/chat/completions", tags=["chat"])
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
) -> Any:
    """OpenAI-compatible chat completions backed by Obscura agents."""
    # Validate model against allowlist before any processing.
    if body.model not in _ALLOWED_MODELS:
        raise HTTPException(
            status_code=400,
            detail={"error": "unknown_model", "allowed": sorted(_ALLOWED_MODELS)},
        )

    # GatewayConfig is stashed on app.state by create_gateway_app
    config = getattr(request.app.state, "gateway_config", None)
    default_backend: str = config.agent_backend if config else "claude"
    default_model: str = config.agent_model if config else ""
    request_timeout: float = config.request_timeout if config else 120.0

    backend = _resolve_backend(body.model, default_backend)
    # Use gateway default model only when the caller didn't pick a specific
    # sub-backend via "obscura/<provider>" syntax.
    effective_model = default_model if body.model in ("obscura", "") else ""

    system_prompt, user_prompt = _extract_prompt(body.messages)

    # Enforce prompt size cap.
    total_len = len(system_prompt.encode()) + len(user_prompt.encode())
    if total_len > _MAX_PROMPT_BYTES:
        raise HTTPException(
            status_code=400,
            detail={"error": "prompt_too_large", "max_bytes": _MAX_PROMPT_BYTES},
        )

    if not user_prompt:
        # Nothing to do — return empty completion
        return ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex}",
            created=int(time.time()),
            model=body.model,
            choices=[
                Choice(
                    message=ChatMessage(role="assistant", content=""),
                    finish_reason="stop",
                )
            ],
        )

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    if body.stream:
        return StreamingResponse(
            _sse_generator(
                backend,
                body.model,
                system_prompt,
                user_prompt,
                completion_id,
                created,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming: run to completion
    try:
        text = await asyncio.wait_for(
            _run_agent(backend, effective_model, system_prompt, user_prompt),
            timeout=request_timeout,
        )
    except TimeoutError:
        text = ""
        logger.warning("Agent run timed out for model=%s", body.model)

    return ChatCompletionResponse(
        id=completion_id,
        created=created,
        model=body.model,
        choices=[
            Choice(
                message=ChatMessage(role="assistant", content=text),
                finish_reason="stop",
            )
        ],
        usage=Usage(
            prompt_tokens=len(user_prompt.split()),
            completion_tokens=len(text.split()),
            total_tokens=len(user_prompt.split()) + len(text.split()),
        ),
    )


__all__ = ["router", "ChatCompletionRequest", "ChatCompletionResponse"]
