"""obscura.integrations.network_gateway.ws — WebSocket chat handler.

Bidirectional WebSocket endpoint at ``WS /v1/chat/ws``.

Protocol (JSON messages)
------------------------

Client → Server::

    {"type": "message", "content": "...", "session_id": "optional-uuid",
     "backend": "claude"}
    {"type": "ping"}

Server → Client::

    {"type": "token",  "content": "...", "session_id": "..."}
    {"type": "done",   "session_id": "...", "usage": {"prompt_tokens": N,
                                                        "completion_tokens": N}}
    {"type": "error",  "message": "...", "code": "..."}
    {"type": "pong"}

Authentication
--------------
``Authorization: Bearer <token>`` header on the WebSocket upgrade request.
Falls back to an ``api_key`` query parameter (browsers cannot set WS headers).
Closes with code **4001** if no valid credential is found.
"""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
import logging
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from obscura.auth.rbac import user_from_api_key
from obscura.integrations.network_gateway.sessions import get_session_store

logger = logging.getLogger(__name__)

# Gateway proxies streamed completions to the local Obscura REST server.
_OBSCURA_BASE_URL = "http://localhost:8080"

_HTTP_OK = 200

# Server-side keepalive interval.
_PING_INTERVAL: float = 30.0

ws_router = APIRouter()


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _extract_bearer(websocket: WebSocket) -> str | None:
    """Extract a raw token from the ``Authorization`` header or ``api_key`` param."""
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip() or None
    # Fallback: query parameter (browser WebSocket API limitation)
    return websocket.query_params.get("api_key") or None


async def _authenticate(websocket: WebSocket) -> bool:
    """Validate credentials on *websocket*.

    Closes the socket with code 4001 and returns ``False`` when authentication
    fails.  Returns ``True`` and leaves the socket open on success.
    """
    token = _extract_bearer(websocket)
    if token and user_from_api_key(token) is not None:
        return True
    logger.warning("Gateway WS auth failed — closing with 4001")
    await websocket.close(code=4001, reason="Unauthorized")
    return False


# ---------------------------------------------------------------------------
# SSE parsing helper
# ---------------------------------------------------------------------------


def _parse_sse_chunk(data: str) -> tuple[str, int, int]:
    """Parse one SSE data payload.

    Returns ``(delta_text, prompt_tokens, completion_tokens)`` from the chunk.
    ``delta_text`` is empty when the chunk carries no content delta.
    """
    with contextlib.suppress(Exception):
        chunk = _json.loads(data)
        delta: str = (
            chunk.get("choices", [{}])[0].get("delta", {}).get("content", "") or ""
        )
        usage: dict[str, Any] = chunk.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        return delta, pt, ct
    return "", 0, 0


# ---------------------------------------------------------------------------
# Proxy streaming call
# ---------------------------------------------------------------------------


async def _read_sse_stream(
    websocket: WebSocket,
    session_id: str,
    payload: dict[str, Any],
    accumulated: list[str],
) -> tuple[int, int]:
    """POST *payload* to the upstream server and stream SSE tokens to *websocket*.

    Returns ``(prompt_tokens, completion_tokens)`` harvested from the stream.
    Raises :exc:`httpx.ConnectError` when the upstream is unreachable.
    """
    prompt_tokens = 0
    completion_tokens = 0

    async with (
        httpx.AsyncClient(timeout=120.0) as client,
        client.stream(
            "POST",
            f"{_OBSCURA_BASE_URL}/v1/chat/completions",
            json=payload,
            headers={"Content-Type": "application/json"},
        ) as response,
    ):
        if response.status_code != _HTTP_OK:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": f"Upstream error {response.status_code}",
                    "code": "upstream_error",
                },
            )
            return prompt_tokens, completion_tokens

        async for raw_line in response.aiter_lines():
            stripped = raw_line.strip()
            if not stripped or not stripped.startswith("data: "):
                continue
            data = stripped[6:]
            if data == "[DONE]":
                break
            delta, pt, ct = _parse_sse_chunk(data)
            if delta:
                accumulated.append(delta)
                await websocket.send_json(
                    {
                        "type": "token",
                        "content": delta,
                        "session_id": session_id,
                    },
                )
            if pt:
                prompt_tokens = pt
            if ct:
                completion_tokens = ct

    return prompt_tokens, completion_tokens


async def _stream_completion(
    websocket: WebSocket,
    session_id: str,
    content: str,
    backend: str,
    history: list[dict[str, Any]],
) -> None:
    """Run one chat turn and stream tokens back over *websocket*.

    Proxies to the upstream ``/v1/chat/completions`` endpoint with
    ``stream=True``.  Sends an error frame when the server is unreachable.
    """
    store = get_session_store()

    messages: list[dict[str, str]] = [
        {"role": str(m["role"]), "content": str(m["content"])} for m in history
    ]
    messages.append({"role": "user", "content": content})

    payload: dict[str, Any] = {"model": backend, "messages": messages, "stream": True}
    accumulated: list[str] = []

    try:
        prompt_tokens, completion_tokens = await _read_sse_stream(
            websocket,
            session_id,
            payload,
            accumulated,
        )
    except httpx.ConnectError:
        await websocket.send_json(
            {
                "type": "error",
                "message": "Local Obscura server not reachable at localhost:8080",
                "code": "upstream_unavailable",
            },
        )
        return
    except Exception as exc:
        logger.exception("Unexpected error in _stream_completion")
        await websocket.send_json(
            {"type": "error", "message": str(exc), "code": "internal_error"},
        )
        return

    assistant_text = "".join(accumulated)

    # Persist turn in session history.
    await store.append(session_id, "user", content)
    if assistant_text:
        await store.append(session_id, "assistant", assistant_text)

    await websocket.send_json(
        {
            "type": "done",
            "session_id": session_id,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        },
    )


# ---------------------------------------------------------------------------
# Message dispatch helper
# ---------------------------------------------------------------------------


async def _dispatch_message(
    websocket: WebSocket,
    raw: dict[str, Any],
    store: Any,
    active_session_ids: set[str],
) -> bool:
    """Process one inbound WebSocket message.

    Returns ``True`` to continue the receive loop, ``False`` to break.
    Handles ``ping``, ``message``, and unknown message types.
    """
    msg_type: str = str(raw.get("type", ""))

    if msg_type == "ping":
        await websocket.send_json({"type": "pong"})
        return True

    if msg_type != "message":
        await websocket.send_json(
            {
                "type": "error",
                "message": f"Unknown message type: {msg_type!r}",
                "code": "unknown_type",
            },
        )
        return True

    content: str = str(raw.get("content", "")).strip()
    if not content:
        await websocket.send_json(
            {
                "type": "error",
                "message": "content must not be empty",
                "code": "empty_content",
            },
        )
        return True

    session_id: str = str(raw.get("session_id") or uuid.uuid4())
    backend: str = str(raw.get("backend") or "copilot")

    active_session_ids.add(session_id)
    history = await store.get_history(session_id)

    await _stream_completion(websocket, session_id, content, backend, history)
    return True


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@ws_router.websocket("/v1/chat/ws")
async def chat_websocket(websocket: WebSocket) -> None:
    """Bidirectional WebSocket for streaming agent conversations.

    See module docstring for the full message protocol.
    """
    # Auth before accept so we can still send a close frame with a code.
    await websocket.accept()

    if not await _authenticate(websocket):
        return  # socket already closed inside _authenticate

    store = get_session_store()

    # Track per-connection sessions so we can clean up on disconnect.
    active_session_ids: set[str] = set()

    async def _keepalive() -> None:
        """Send a server-side ping every _PING_INTERVAL seconds."""
        while True:
            await asyncio.sleep(_PING_INTERVAL)
            with contextlib.suppress(Exception):
                await websocket.send_json({"type": "ping"})

    keepalive_task = asyncio.create_task(_keepalive())

    try:
        while True:
            raw = await websocket.receive_json()
            await _dispatch_message(websocket, raw, store, active_session_ids)

    except WebSocketDisconnect:
        logger.debug("Gateway WS client disconnected ids=%s", active_session_ids)
    except Exception:
        logger.exception("Unhandled error in chat_websocket")
    finally:
        keepalive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive_task
        # Clean up all sessions owned by this connection.
        for sid in active_session_ids:
            await store.clear(sid)
