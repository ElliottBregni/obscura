"""obscura.integrations.network_gateway.standalone_agent — Standalone agent server.

A lightweight FastAPI app that runs Obscura agents directly over WebSocket
and OpenAI-compatible HTTP, analogous to OpenClaw's gateway but for Obscura.

Endpoints
---------
* ``WS  /ws``                — streaming chat WebSocket (JSON + plain-text)
* ``POST /v1/chat/completions`` — OpenAI-compatible completions
* ``GET  /v1/models``           — list Obscura backends
* ``GET  /health``              — unauthenticated liveness probe
* ``GET  /``                    — embedded HTML chat UI

Auth
----
Challenge-response handshake on every new WS connection:
  1. Server sends ``{"type":"connect.challenge","nonce":"<hex>"}``
  2. Client replies ``{"type":"connect","token":"<raw>"}`` within 10 s
  3. Server validates; closes 4001 on failure
  4. Server sends ``{"type":"connect.ok","conn_id":"<id>"}`` on success

API clients may skip the challenge by setting ``Authorization: Bearer <token>``
on the upgrade request.  ``/health`` is always public.

Usage::

    from obscura.integrations.network_gateway.standalone_agent import create_standalone_agent_app
    from obscura.integrations.network_gateway.config import GatewayConfig

    app = create_standalone_agent_app(GatewayConfig())
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac as _hmac
import json as _json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from obscura.auth.security_headers import SecurityHeadersMiddleware
from obscura.integrations.network_gateway.auth import (
    GatewayBearerAuthMiddleware,
    GatewayRateLimitMiddleware,
)
from obscura.integrations.network_gateway.chat_completions import (
    _stream_agent,
    router as chat_router,
)
from obscura.integrations.network_gateway.config import GatewayConfig
from obscura.integrations.network_gateway.connections import (
    PROTOCOL_MIN_SUPPORTED,
    PROTOCOL_VERSION,
    get_registry,
)
from obscura.integrations.network_gateway.models import router as models_router
from obscura.integrations.network_gateway.sessions import get_session_store
from obscura.routes.channels import router as channels_router

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Embedded HTML chat UI
# ---------------------------------------------------------------------------

_CHAT_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Obscura Agent</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0d1117; color: #e6edf3;
    font-family: 'Courier New', monospace;
    display: flex; flex-direction: column; height: 100vh;
  }
  #header {
    padding: 12px 16px; border-bottom: 1px solid #21262d;
    display: flex; justify-content: space-between; align-items: center;
  }
  #header h1 { font-size: 14px; color: #58a6ff; letter-spacing: 0.05em; }
  #session-label { font-size: 11px; color: #6e7681; }
  #messages {
    flex: 1; overflow-y: auto; padding: 16px;
    display: flex; flex-direction: column; gap: 12px;
  }
  .msg { max-width: 85%; padding: 10px 14px; border-radius: 6px; line-height: 1.5; font-size: 13px; }
  .msg.user { background: #1c2a3e; border: 1px solid #264a74; align-self: flex-end; color: #79c0ff; }
  .msg.assistant { background: #161b22; border: 1px solid #21262d; align-self: flex-start; white-space: pre-wrap; }
  .msg.thinking { color: #6e7681; font-style: italic; }
  .msg.incoming {
    background: #1c2a1c; border: 1px solid #2d4a1e;
    border-left: 3px solid #3fb950;
    align-self: flex-start;
  }
  .msg.incoming .platform-badge {
    font-size: 11px; color: #3fb950; margin-bottom: 4px; display: block;
  }
  .msg.incoming .reply-hint {
    font-size: 11px; color: #6e7681; margin-top: 6px; display: block;
  }
  #footer { padding: 12px 16px; border-top: 1px solid #21262d; display: flex; gap: 8px; }
  #input {
    flex: 1; background: #161b22; border: 1px solid #30363d; border-radius: 6px;
    color: #e6edf3; padding: 10px 12px; font-family: inherit; font-size: 13px;
    resize: none; min-height: 44px; max-height: 140px;
  }
  #input:focus { outline: none; border-color: #388bfd; }
  #send {
    background: #238636; color: #fff; border: none; border-radius: 6px;
    padding: 10px 18px; font-family: inherit; font-size: 13px;
    cursor: pointer; align-self: flex-end;
  }
  #send:hover { background: #2ea043; }
  #send:disabled { background: #21262d; color: #6e7681; cursor: not-allowed; }
  #status { font-size: 11px; color: #6e7681; padding: 4px 16px; }
</style>
</head>
<body>
<div id="header">
  <h1>&#9670; Obscura Agent</h1>
  <span id="session-label">session: —</span>
</div>
<div id="messages"></div>
<div id="status"></div>
<div id="footer">
  <textarea id="input" placeholder="Send a message..." rows="1"></textarea>
  <button id="send">Send</button>
</div>
<script>
  const messages = document.getElementById('messages');
  const input = document.getElementById('input');
  const sendBtn = document.getElementById('send');
  const statusEl = document.getElementById('status');
  const sessionLabel = document.getElementById('session-label');

  let ws = null;
  let sessionId = null;
  let currentMsg = null;
  let thinking = null;
  let unreadCount = 0;

  function escapeHtml(text) {
    const d = document.createElement('div');
    d.appendChild(document.createTextNode(text));
    return d.innerHTML;
  }

  window.addEventListener('focus', () => {
    unreadCount = 0;
    document.title = 'Obscura Agent';
  });

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = proto + '//' + location.host + '/ws';
    ws = new WebSocket(url);

    ws.onopen = () => { statusEl.textContent = 'authenticating...'; };
    ws.onclose = () => {
      statusEl.textContent = 'disconnected — reconnecting...';
      setTimeout(connect, 2000);
    };
    ws.onerror = () => { statusEl.textContent = 'connection error'; };

    ws.onmessage = (e) => {
      let frame;
      try { frame = JSON.parse(e.data); } catch { return; }

      if (frame.type === 'connect.challenge') {
        const params = new URLSearchParams(location.search);
        const tok = params.get('token') || params.get('api_key') || '';
        ws.send(JSON.stringify({type: 'connect', token: tok}));
        return;
      }
      if (frame.type === 'connect.ok') {
        statusEl.textContent = 'ready';
        return;
      }

      if (frame.type === 'pong') return;

      if (frame.type === 'token') {
        if (thinking) { thinking.remove(); thinking = null; }
        if (!currentMsg) {
          currentMsg = document.createElement('div');
          currentMsg.className = 'msg assistant';
          messages.appendChild(currentMsg);
        }
        currentMsg.textContent += frame.content;
        if (frame.session_id) {
          sessionId = frame.session_id;
          sessionLabel.textContent = 'session: ' + sessionId.slice(0, 8);
        }
        messages.scrollTop = messages.scrollHeight;
      } else if (frame.type === 'done') {
        currentMsg = null;
        sendBtn.disabled = false;
        input.disabled = false;
        input.focus();
        statusEl.textContent = 'ready';
      } else if (frame.type === 'error') {
        if (thinking) { thinking.remove(); thinking = null; }
        const err = document.createElement('div');
        err.className = 'msg assistant thinking';
        err.textContent = 'Error: ' + (frame.message || frame.code || 'unknown');
        messages.appendChild(err);
        currentMsg = null;
        sendBtn.disabled = false;
        input.disabled = false;
        statusEl.textContent = 'error';
      } else if (frame.type === 'incoming') {
        const platformIcons = {whatsapp:'📱', telegram:'✈️', imessage:'🍎', signal:'🔒', sms:'💬'};
        const icon = platformIcons[frame.platform] || '💬';
        const div = document.createElement('div');
        div.className = 'msg incoming';
        div.innerHTML = `<span class="platform-badge">${icon} ${escapeHtml(frame.platform)} · ${escapeHtml(frame.sender)}</span>${escapeHtml(frame.text)}<span class="reply-hint">↩ reply to respond via ${escapeHtml(frame.platform)}</span>`;
        messages.appendChild(div);
        messages.scrollTop = messages.scrollHeight;
        document.title = `(${++unreadCount}) Obscura Agent`;
      } else if (frame.type === 'agent') {
        if (frame.state === 'running') {
          statusEl.textContent = 'agent running…';
          statusEl.style.color = '#f0a500';
        } else {
          statusEl.textContent = 'ready';
          statusEl.style.color = '';
        }
      } else if (frame.type === 'presence') {
        statusEl.textContent = frame.count === 1 ? '1 client connected' : frame.count + ' clients connected';
      } else if (frame.type === 'health') {
        sessionLabel.textContent = 'v' + frame.version + ' · ' + frame.connections + ' connected';
      }
    };
  }

  function send() {
    const text = input.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;

    const userDiv = document.createElement('div');
    userDiv.className = 'msg user';
    userDiv.textContent = text;
    messages.appendChild(userDiv);

    thinking = document.createElement('div');
    thinking.className = 'msg assistant thinking';
    thinking.textContent = 'thinking...';
    messages.appendChild(thinking);
    messages.scrollTop = messages.scrollHeight;

    const frame = { type: 'message', content: text };
    if (sessionId) frame.session_id = sessionId;
    ws.send(JSON.stringify(frame));

    input.value = '';
    input.style.height = 'auto';
    sendBtn.disabled = true;
    input.disabled = true;
    statusEl.textContent = 'running...';
  }

  sendBtn.addEventListener('click', send);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  });

  connect();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# WebSocket auth helper — challenge-response
# ---------------------------------------------------------------------------


async def _sa_challenge_auth(websocket: WebSocket, token: str) -> tuple[bool, str | None]:
    """Challenge-response auth for standalone agent WS. Returns ``(True, resume_sid)`` on success.

    Flow:
      1. Send ``{"type":"connect.challenge","nonce":"<uuid>"}``
      2. Receive ``{"type":"connect","token":"<raw>"}`` (10 s timeout)
      3. If token configured: validate with ``hmac.compare_digest``
      4. Return ``(True, resume_sid)`` (success) or close 4001 and return ``(False, None)``

    When *token* is empty (open mode) skip validation and return ``(True, resume_sid)``.
    API clients that set ``Authorization: Bearer`` on the upgrade request skip
    the challenge entirely (legacy / non-browser path).
    """
    import uuid as _uuid

    # Legacy: honour Authorization header for API clients that set WS headers.
    if token:
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            raw = auth_header[7:].strip()
            if raw and _hmac.compare_digest(raw, token):
                return (True, None)

    nonce = _uuid.uuid4().hex
    try:
        await websocket.send_json({"type": "connect.challenge", "nonce": nonce})
        raw_frame = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
    except Exception:
        await websocket.close(code=4001, reason="Handshake timeout")
        return (False, None)

    if not isinstance(raw_frame, dict) or raw_frame.get("type") != "connect":
        await websocket.close(code=4001, reason="Expected connect frame")
        return (False, None)

    # Protocol version negotiation
    min_proto = int(raw_frame.get("minProtocol", 1))
    max_proto = int(raw_frame.get("maxProtocol", 1))
    if max_proto < PROTOCOL_MIN_SUPPORTED or min_proto > PROTOCOL_VERSION:
        logger.warning(
            "Standalone agent WS protocol mismatch: client=[%d,%d] server=%d",
            min_proto, max_proto, PROTOCOL_VERSION,
        )
        await websocket.close(code=4002, reason=f"Protocol mismatch: server={PROTOCOL_VERSION}")
        return (False, None)

    if token:
        provided = str(raw_frame.get("token", ""))
        if not provided or not _hmac.compare_digest(provided, token):
            logger.warning("Standalone agent WS challenge-response auth failed")
            await websocket.close(code=4001, reason="Unauthorized")
            return (False, None)

    resume_session_id: str | None = raw_frame.get("resume_session_id") or None
    return (True, resume_session_id)


# ---------------------------------------------------------------------------
# WebSocket streaming handler
# ---------------------------------------------------------------------------


async def _sa_stream_completion(
    websocket: WebSocket,
    session_id: str,
    content: str,
    backend: str,
    model: str,
) -> str:
    """Run one agent turn and stream tokens back over *websocket*.

    Returns the full assistant response text (empty string on error).
    """
    store = get_session_store()
    accumulated: list[str] = []

    try:
        async for delta in _stream_agent(backend, model, "", content):
            accumulated.append(delta)
            await websocket.send_json(
                {"type": "token", "content": delta, "session_id": session_id}
            )
    except Exception as exc:
        logger.exception("Standalone agent error during stream")
        await websocket.send_json(
            {"type": "error", "message": str(exc), "code": "internal_error"}
        )
        return ""

    assistant_text = "".join(accumulated)

    # Persist turn in session history.
    await store.append(session_id, "user", content)
    if assistant_text:
        await store.append(session_id, "assistant", assistant_text)

    await websocket.send_json({"type": "done", "session_id": session_id, "usage": {}})
    return assistant_text


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_standalone_agent_app(config: GatewayConfig | None = None) -> FastAPI:
    """Build and return the Obscura standalone agent :class:`~fastapi.FastAPI` app.

    Parameters
    ----------
    config:
        Gateway configuration (reuses :class:`GatewayConfig`). Defaults to
        a fresh ``GatewayConfig()`` instance when ``None``.

    Returns
    -------
    FastAPI
        Fully configured application ready to be served with uvicorn on port
        18792 (default).
    """
    if config is None:
        config = GatewayConfig()

    # ------------------------------------------------------------------
    # Lifespan: wire messaging platform adapters into the ChannelRouter
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _sa_lifespan(app: FastAPI):  # type: ignore[type-arg]
        """Start-up: load messaging platforms from config and wire the ChannelRouter."""
        try:
            from obscura.integrations.messaging.platform_loader import (
                load_messaging_platforms,
            )
            from obscura.integrations.messaging.router import (
                ChannelRouter,
                ChannelRouterConfig,
            )
            from obscura.integrations.messaging.runners import ObscuraAgentRunner
            from obscura.integrations.messaging.store import ChannelConfigRecord
            from obscura.core.enums.messaging import ChannelMode
            from obscura.routes.channels import init_channel_router
            import hashlib
            import time

            platforms = load_messaging_platforms()

            # ObscuraAgentRunner requires backend + tool_registry.
            # The standalone agent doesn't maintain a persistent tool registry here;
            # pass None — the runner will operate without extra tools beyond what the
            # backend itself provides.  Callers that need a full registry should wire
            # it separately via the REST config API.
            runner = ObscuraAgentRunner(
                backend=config.agent_backend,
                tool_registry=None,
            )
            ch_router = ChannelRouter(
                runner=runner,
                config=ChannelRouterConfig(mode=ChannelMode.CHANNEL_INJECT),
            )

            loaded_count = 0
            for pconf in platforms:
                if not pconf.get("enabled", True):
                    logger.debug(
                        "Standalone agent: skipping disabled platform=%s",
                        pconf.get("platform"),
                    )
                    continue
                try:
                    platform_id = str(pconf.get("platform", "unknown"))
                    record = ChannelConfigRecord.from_dict(
                        {
                            "id": hashlib.md5(platform_id.encode()).hexdigest(),
                            "platform": platform_id,
                            "label": pconf.get("label", platform_id),
                            "enabled": pconf.get("enabled", True),
                            "mode": pconf.get("mode", "channel_inject"),
                            "credentials": pconf.get("credentials", {}),
                            "contacts": pconf.get("contacts", []),
                            "router_config": {},
                            "created_at_epoch_s": time.time(),
                            "updated_at_epoch_s": time.time(),
                        }
                    )
                    await ch_router.apply_config(record)
                    loaded_count += 1
                except Exception:
                    logger.warning(
                        "Standalone agent: failed to apply config for platform=%s",
                        pconf.get("platform"),
                        exc_info=True,
                    )

            init_channel_router(ch_router)
            logger.info(
                "Standalone agent: messaging platforms loaded — %d active",
                loaded_count,
            )

        except Exception:
            logger.warning(
                "Standalone agent: messaging platform wiring failed — "
                "channels will be unavailable",
                exc_info=True,
            )

        yield  # app runs here

    app = FastAPI(
        title="Obscura Standalone Agent",
        description="Direct Obscura agent server for remote chat (Tailscale / LAN).",
        version="0.7.0",
        docs_url=None,
        redoc_url=None,
        lifespan=_sa_lifespan,
    )

    # Stash config so route handlers can reach it.
    app.state.gateway_config = config

    # -- Middleware (LIFO registration — innermost first) --------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.add_middleware(GatewayBearerAuthMiddleware, token=config.token)
    app.add_middleware(GatewayRateLimitMiddleware, max_requests=config.rate_limit)
    app.add_middleware(SecurityHeadersMiddleware)

    # -- Health (unauthenticated) -------------------------------------------

    resolved_port = config.standalone_agent_port

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """Unauthenticated liveness probe."""
        return {
            "status": "ok",
            "service": "obscura-standalone-agent",
            "port": resolved_port,
        }

    # -- Peers endpoint (connected WS clients + REPL sessions) -------------

    @app.get("/v1/peers", tags=["peers"])
    async def list_peers() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """List connected WS clients and active REPL sessions."""
        reg = get_registry()
        ws_clients = reg.snapshot()
        repl_sessions: list[dict[str, str]] = []
        try:
            from obscura.kairos.uds_messaging import discover_peers
            repl_sessions = [{"session_id": sid, "type": "repl"} for sid in discover_peers()]
        except Exception:
            pass
        return {"ws_clients": ws_clients, "repl_sessions": repl_sessions, "total": len(ws_clients) + len(repl_sessions)}

    # -- Embedded chat UI --------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def chat_ui() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        """Minimal embedded chat UI for direct browser use."""
        resp = HTMLResponse(_CHAT_UI_HTML)
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' wss: ws:; "
            "img-src 'self' data:; "
            "frame-ancestors 'none'"
        )
        return resp

    # -- OpenAI-compatible /v1 routes --------------------------------------

    app.include_router(chat_router)
    app.include_router(models_router)

    # -- Messaging channel webhook + config CRUD routes --------------------

    app.include_router(channels_router)

    # -- WebSocket streaming chat ------------------------------------------

    _sa_token = config.token
    _sa_backend = config.agent_backend
    _sa_model = config.agent_model
    _sa_ping_interval = config.ws_ping_interval

    @app.websocket("/ws")
    async def standalone_ws(websocket: WebSocket) -> None:  # pyright: ignore[reportUnusedFunction]
        """Bidirectional streaming WebSocket chat.

        Accepts both JSON frames and plain text.  JSON frames may be::

            {"type": "message", "content": "...", "session_id": "opt"}
            {"type": "ping"}

        Plain text is treated as a bare message content.
        """
        await websocket.accept()

        _auth_ok, _resume_sid = await _sa_challenge_auth(websocket, _sa_token)
        if not _auth_ok:
            return

        store = get_session_store()

        # Register with the process-level ConnectionRegistry and announce presence.
        registry = get_registry()
        conn_id = await registry.register(
            websocket,
            endpoint="/ws",
            remote=websocket.client.host if websocket.client else "",
        )

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

        await registry.broadcast_presence("connected", conn_id, endpoint="/ws", exclude=conn_id)
        await registry.send_health(websocket)

        async def _keepalive() -> None:
            while True:
                await asyncio.sleep(_sa_ping_interval)
                with contextlib.suppress(Exception):
                    await websocket.send_json({"type": "ping"})

        keepalive_task = asyncio.create_task(_keepalive())

        try:
            while True:
                raw_text = await websocket.receive_text()

                # Try to parse as JSON; fall back to treating as plain-text message.
                frame: dict[str, Any]
                try:
                    frame = _json.loads(raw_text)
                    if not isinstance(frame, dict):
                        frame = {"type": "message", "content": raw_text}
                except _json.JSONDecodeError:
                    frame = {"type": "message", "content": raw_text}

                msg_type: str = str(frame.get("type", ""))

                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue

                if msg_type != "message":
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": f"Unknown message type: {msg_type!r}",
                            "code": "unknown_type",
                        }
                    )
                    continue

                content: str = str(frame.get("content", "")).strip()
                if not content:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": "content must not be empty",
                            "code": "empty_content",
                        }
                    )
                    continue

                session_id: str = str(frame.get("session_id") or uuid.uuid4())
                active_session_ids.add(session_id)

                await registry.broadcast_agent_state("running", conn_id=conn_id)
                assistant_text = await _sa_stream_completion(
                    websocket,
                    session_id,
                    content,
                    _sa_backend,
                    _sa_model,
                )
                await registry.broadcast_agent_state("idle", conn_id=conn_id)

                # If a platform message was pending, route the reply back to its origin.
                reply_fn = registry.pop_active_reply()
                if reply_fn is not None and assistant_text:
                    try:
                        await reply_fn(assistant_text)
                    except Exception:
                        logger.exception("Failed to send reply to platform via reply_fn")

        except WebSocketDisconnect:
            logger.debug("Standalone agent WS disconnected ids=%s", active_session_ids)
        except Exception:
            logger.exception("Unhandled error in standalone_ws")
        finally:
            keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive_task
            await registry.unregister(conn_id)
            await registry.broadcast_presence("disconnected", conn_id)
            for sid in active_session_ids:
                await store.clear(sid)

    logger.info(
        "Standalone agent configured: host=%s port=%d backend=%s auth=%s",
        config.standalone_agent_host,
        config.standalone_agent_port,
        config.agent_backend,
        "enabled" if config.token else "disabled",
    )

    return app


__all__ = ["create_standalone_agent_app"]
