"""obscura.integrations.network_gateway.ws — WebSocket chat handler.

Bidirectional WebSocket endpoint at ``WS /v1/chat/ws``.

Protocol (JSON messages)
------------------------

Client → Server::

    {"type": "message", "content": "...", "session_id": "optional-uuid",
     "backend": "claude"}
    {"type": "ping"}

Server → Client::

    {"type": "connect.challenge", "nonce": "<hex>"}
    {"type": "connect.ok",        "conn_id": "<id>"}
    {"type": "health",            "status": "ok", "version": "...", "connections": N}
    {"type": "token",             "content": "...", "session_id": "..."}
    {"type": "done",              "session_id": "...", "usage": {"prompt_tokens": N,
                                                                  "completion_tokens": N}}
    {"type": "error",             "message": "...", "code": "..."}
    {"type": "pong"}

Authentication
--------------
Challenge-response handshake on every new connection:
  1. Server sends ``{"type": "connect.challenge", "nonce": "<uuid-hex>"}``
  2. Client replies ``{"type": "connect", "token": "<raw>"}`` within 10 s
  3. Server validates (HMAC-safe compare); closes 4001 on failure
  4. Server sends ``{"type": "connect.ok", "conn_id": "<id>"}`` on success

API clients that set the ``Authorization: Bearer <token>`` header on the
upgrade request skip the challenge exchange (legacy / non-browser path).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from obscura.integrations.network_gateway.auth import _client_ip
from obscura.integrations.network_gateway.chat_completions import _stream_agent
from obscura.integrations.network_gateway.connections import (
    PROTOCOL_MIN_SUPPORTED,
    PROTOCOL_VERSION,
    get_registry,
)
from obscura.integrations.network_gateway.sessions import get_session_store

logger = logging.getLogger(__name__)

ws_router = APIRouter()


# ---------------------------------------------------------------------------
# Auth helper — challenge-response
# ---------------------------------------------------------------------------


async def _challenge_auth(
    websocket: WebSocket, token: str
) -> tuple[str | None, str | None]:
    """Challenge-response auth. Returns ``(None, resume_session_id)`` on success, ``("failed", None)`` on failure.

    Flow:
      1. Send ``{"type":"connect.challenge","nonce":"<uuid>"}``
      2. Receive ``{"type":"connect","token":"<raw>"}`` (10 s timeout)
      3. If token configured: validate with ``hmac.compare_digest``
      4. Return ``(None, resume_sid)`` (success) or close 4001 and return ``("failed", None)``

    When *token* is empty (open mode) skip validation and return ``(None, resume_sid)``.
    API clients that set ``Authorization: Bearer`` on the upgrade request skip
    the challenge entirely (legacy / non-browser path).
    """
    import hmac as _hmac
    import uuid as _uuid

    # Legacy: honour Authorization header for API clients that set WS headers.
    if token:
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            raw = auth_header[7:].strip()
            if raw and _hmac.compare_digest(raw, token):
                return (
                    None,
                    None,
                )  # skip challenge; caller will register + get conn_id

    nonce = _uuid.uuid4().hex
    try:
        await websocket.send_json({"type": "connect.challenge", "nonce": nonce})
        raw_frame = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
    except (asyncio.TimeoutError, Exception):
        await websocket.close(code=4001, reason="Handshake timeout")
        return ("failed", None)

    if not isinstance(raw_frame, dict) or raw_frame.get("type") != "connect":
        await websocket.close(code=4001, reason="Expected connect frame")
        return ("failed", None)

    # Protocol version negotiation
    min_proto = int(raw_frame.get("minProtocol", 1))
    max_proto = int(raw_frame.get("maxProtocol", 1))
    if max_proto < PROTOCOL_MIN_SUPPORTED or min_proto > PROTOCOL_VERSION:
        logger.warning(
            "Gateway WS protocol mismatch: client=[%d,%d] server=%d",
            min_proto,
            max_proto,
            PROTOCOL_VERSION,
        )
        await websocket.close(
            code=4002, reason=f"Protocol mismatch: server={PROTOCOL_VERSION}"
        )
        return ("failed", None)

    if token:
        provided = str(raw_frame.get("token", ""))
        if not provided or not _hmac.compare_digest(provided, token):
            logger.warning("Gateway WS challenge-response auth failed")
            await websocket.close(code=4001, reason="Unauthorized")
            return ("failed", None)

    resume_session_id: str | None = raw_frame.get("resume_session_id") or None
    return (None, resume_session_id)  # success


# ---------------------------------------------------------------------------
# Direct agent streaming
# ---------------------------------------------------------------------------


async def _stream_completion(
    websocket: WebSocket,
    session_id: str,
    content: str,
    backend: str,
    history: list[dict[str, Any]],
    *,
    request_timeout: float = 120.0,
) -> None:
    """Run one chat turn and stream tokens back over *websocket*.

    Executes the agent directly via :func:`_stream_agent` — no proxy to a
    local REST server.  Sends an error frame on failure.
    """
    store = get_session_store()
    accumulated: list[str] = []

    try:
        async for delta in _stream_agent(backend, backend, "", content):
            accumulated.append(delta)
            await websocket.send_json(
                {"type": "token", "content": delta, "session_id": session_id},
            )
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
            "usage": {},
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
    *,
    request_timeout: float = 120.0,
    registry: Any = None,
    conn_id: str | None = None,
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

    if registry is not None:
        await registry.broadcast_agent_state("running", conn_id=conn_id)
    await _stream_completion(
        websocket,
        session_id,
        content,
        backend,
        history,
        request_timeout=request_timeout,
    )
    if registry is not None:
        await registry.broadcast_agent_state("idle", conn_id=conn_id)
    return True


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@ws_router.websocket("/v1/chat/ws")
async def chat_websocket(websocket: WebSocket) -> None:
    """Bidirectional WebSocket for streaming agent conversations.

    See module docstring for the full message protocol.
    """
    # Accept first (required by ASGI), then run challenge-response auth.
    await websocket.accept()

    gw_config = getattr(websocket.app.state, "gateway_config", None)
    _token: str = gw_config.token if gw_config else ""
    _auth_result, _resume_sid = await _challenge_auth(websocket, _token)
    if _auth_result == "failed":
        return

    # Read timing config from GatewayConfig stashed on app.state.
    ping_interval: float = gw_config.ws_ping_interval if gw_config else 30.0
    request_timeout: float = gw_config.request_timeout if gw_config else 120.0

    store = get_session_store()

    # Register with the process-level ConnectionRegistry and announce presence.
    registry = get_registry()
    remote = _client_ip(websocket)
    conn_id = await registry.register(websocket, endpoint="/v1/chat/ws", remote=remote)

    # Build connect.ok — optionally resume a previous session.
    active_session_ids: set[str] = set()
    connect_ok: dict[str, Any] = {
        "type": "connect.ok",
        "conn_id": conn_id,
        "protocol": PROTOCOL_VERSION,
    }
    if _resume_sid:
        _history = await store.get_history(_resume_sid)
        if _history:
            connect_ok["resumed_session_id"] = _resume_sid
            connect_ok["history"] = _history
            active_session_ids.add(_resume_sid)
    await websocket.send_json(connect_ok)

    await registry.broadcast_presence(
        "connected", conn_id, endpoint="/v1/chat/ws", exclude=conn_id
    )
    await registry.send_health(websocket)

    async def _keepalive() -> None:
        """Send a server-side ping every ping_interval seconds."""
        while True:
            await asyncio.sleep(ping_interval)
            with contextlib.suppress(Exception):
                await websocket.send_json({"type": "ping"})

    keepalive_task = asyncio.create_task(_keepalive())

    try:
        while True:
            raw = await websocket.receive_json()
            await _dispatch_message(
                websocket,
                raw,
                store,
                active_session_ids,
                request_timeout=request_timeout,
                registry=registry,
                conn_id=conn_id,
            )

    except WebSocketDisconnect:
        logger.debug("Gateway WS client disconnected ids=%s", active_session_ids)
    except Exception:
        logger.exception("Unhandled error in chat_websocket")
    finally:
        keepalive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive_task
        await registry.unregister(conn_id)
        await registry.broadcast_presence("disconnected", conn_id)
        # Clean up all sessions owned by this connection.
        for sid in active_session_ids:
            await store.clear(sid)
