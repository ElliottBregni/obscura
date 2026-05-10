"""obscura.integrations.network_gateway.app — FastAPI gateway factory.

Exposes Obscura agents over HTTP on port 18790 with:

* ``POST /v1/chat/completions`` — OpenAI-compatible chat completions
* ``GET  /v1/models``           — list Obscura backends as model objects
* ``WS   /v1/chat/ws``          — bidirectional streaming WebSocket chat
* A2A routers at ``/a2a/``      — full A2A protocol (JSON-RPC, REST, SSE)
* ``GET  /health``              — unauthenticated liveness probe
* ``GET  /.well-known/agent.json`` — A2A discovery (always public)

Middleware stack (outermost → innermost, request direction):

    RequestSizeLimit → SecurityHeaders → GatewayRateLimit → GatewayBearerAuth → CORS → routes

Security note: ``/health`` and ``/.well-known/`` are exempt from both auth
and rate limiting.  All other paths require a valid bearer token when one is
configured.

Entry point::

    uvicorn obscura.integrations.network_gateway.app:app \
        --host 0.0.0.0 --port 18790

Or programmatically::

    from obscura.integrations.network_gateway.app import create_gateway_app
    from obscura.integrations.network_gateway.config import GatewayConfig

    app = create_gateway_app(GatewayConfig())
"""

from __future__ import annotations

import hmac as _hmac
import json as _json
import logging
import re as _re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from obscura.auth.security_headers import SecurityHeadersMiddleware
from obscura.integrations.network_gateway.auth import (
    GatewayBearerAuthMiddleware,
    GatewayRateLimitMiddleware,
    RequestSizeLimitMiddleware,
    _client_ip,
)
from obscura.integrations.network_gateway.chat_completions import (
    router as chat_router,
)
from obscura.integrations.network_gateway.config import GatewayConfig
from obscura.integrations.network_gateway.models import router as models_router
from obscura.integrations.network_gateway.sessions import init_session_store
from obscura.integrations.network_gateway.ws import ws_router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Manage embedded A2A server lifecycle alongside the gateway."""
    from obscura.integrations.network_gateway.tailscale import (
        configure_tailscale_serve,
        detect_tailscale_url,
        remove_tailscale_serve,
    )

    a2a_server = getattr(app.state, "a2a_server", None)
    gateway_config: GatewayConfig | None = getattr(app.state, "gateway_config", None)

    if a2a_server is not None:
        await a2a_server.startup()

    # Initialise session store with configured TTL before any WS connections.
    session_ttl = gateway_config.session_ttl if gateway_config is not None else 3600.0
    init_session_store(session_ttl)

    # Tailscale serve — expose gateway to tailnet peers
    _tailscale_active = False
    if gateway_config is not None and gateway_config.tailscale_enabled:
        _tailscale_active = await configure_tailscale_serve(gateway_config.port)
        if _tailscale_active:
            ts_url = (
                detect_tailscale_url()
                or gateway_config.tailscale_url
                or "<tailscale-url>"
            )
            logger.info("Gateway also reachable at %s", ts_url)

    try:
        yield
    finally:
        if _tailscale_active and gateway_config is not None:
            await remove_tailscale_serve(gateway_config.port)
        if a2a_server is not None:
            await a2a_server.shutdown()


def create_gateway_app(config: GatewayConfig | None = None) -> FastAPI:
    """Build and return the Obscura network gateway :class:`~fastapi.FastAPI` app.

    Parameters
    ----------
    config:
        Gateway configuration.  Defaults to :class:`GatewayConfig` with all
        fields at their defaults when ``None`` is passed.

    Returns
    -------
    FastAPI
        Fully configured application ready to be served with uvicorn.

    Security middleware stack (outermost → innermost):
        1. ``RequestSizeLimitMiddleware``
           (rejects oversized bodies before auth/rate-limit)
        2. ``SecurityHeadersMiddleware``
        3. ``GatewayRateLimitMiddleware``
           (60 req/min per IP; exempt: ``/health``, ``/.well-known/``)
        4. ``GatewayBearerAuthMiddleware``
           (when token configured; exempt: ``/health``, ``/.well-known/``)
        5. CORS

    """
    from obscura.integrations.a2a.definition import DEFAULT_AGENT_DEFINITION
    from obscura.integrations.a2a.server import ObscuraA2AServer
    from obscura.integrations.a2a.transports import (
        create_jsonrpc_router,
        create_rest_router,
        create_sse_router,
        create_wellknown_router,
    )

    if config is None:
        config = GatewayConfig()

    base_url = f"http://{config.host}:{config.port}"

    # ----- Embedded A2A server -----
    card = DEFAULT_AGENT_DEFINITION.to_agent_card(base_url)
    a2a_server = ObscuraA2AServer(
        agent_card=card,
        agent_backend=config.agent_backend,
        agent_model=config.agent_model,
    )

    app = FastAPI(
        title="Obscura Network Gateway",
        description=(
            "OpenAI-compatible + A2A gateway for Obscura AI agents. Runs on port 18790."
        ),
        version="0.7.0",
        docs_url="/docs" if config.debug else None,
        redoc_url="/redoc" if config.debug else None,
        lifespan=_lifespan,
    )

    # Stash config + A2A server on state so route handlers can access them.
    app.state.gateway_config = config
    app.state.a2a_server = a2a_server
    app.state.webhook_secret = config.webhook_secret

    # -- Middleware stack (add_middleware wraps in LIFO order) ---------------
    # Desired inbound order:
    #   RequestSizeLimit → SecurityHeaders → GatewayRateLimit → GatewayBearerAuth → CORS → routes
    # Register innermost first (CORS), outermost last (RequestSizeLimit).

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "A2A-Version"],
        expose_headers=[],  # don't leak internal response headers to JS callers
    )

    # Bearer auth (token from GatewayConfig.token; empty = no auth)
    app.add_middleware(GatewayBearerAuthMiddleware, token=config.token)

    # Per-IP sliding-window rate limiter
    app.add_middleware(GatewayRateLimitMiddleware, max_requests=config.rate_limit)

    # Security headers
    app.add_middleware(SecurityHeadersMiddleware)

    # Request body size limit (outermost — catches oversized requests first)
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=config.max_request_bytes)

    # -- Unauthenticated health probe ---------------------------------------

    resolved_config = config

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """Unauthenticated liveness probe."""
        return {
            "status": "ok",
            "service": "obscura-network-gateway",
            "port": resolved_config.port,
        }

    # -- Synthetic peer cards (unauthenticated) ----------------------------

    @app.get(
        "/peers/openclaw/.well-known/agent.json",
        tags=["peers"],
        include_in_schema=False,
    )
    async def openclaw_peer_card() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """Synthetic A2A agent card for OpenClaw (bridge peer).

        OpenClaw speaks OpenAI-compat only and has no A2A server.  This
        endpoint lets any A2A client on the network discover OpenClaw's
        capabilities via Obscura without OpenClaw needing to implement A2A.
        """
        from obscura.integrations.a2a.openclaw_bridge import openclaw_synthetic_card

        card = openclaw_synthetic_card()
        return card.model_dump(mode="json")

    # -- OpenAI-compatible /v1 routes --------------------------------------

    app.include_router(chat_router)
    app.include_router(models_router)

    # -- WebSocket chat ----------------------------------------------------

    app.include_router(ws_router)

    # -- A2A protocol routes -----------------------------------------------

    service = a2a_server.service
    app.include_router(create_jsonrpc_router(service))
    app.include_router(create_rest_router(service))
    app.include_router(create_sse_router(service))
    app.include_router(create_wellknown_router(service))

    # -- Push-notification webhook (public, no auth) -----------------------
    # OpenClaw POSTs completed task results here when pushNotificationUrl is set.

    @app.post("/webhook/a2a", tags=["webhook"])
    async def webhook_a2a(request: Request) -> JSONResponse:
        """Receive A2A push-notification callbacks from peer agents (e.g. OpenClaw).

        Extracts the task result text and broadcasts it to all active REPL
        sessions via UDS so it appears as a peer message in the running
        interactive session(s).

        The gateway runs in a separate process from the REPL; the in-process
        ``channel_inject`` queue is not shared across processes.  UDS
        (``obscura.kairos.uds_messaging``) is the cross-process channel.

        Security: when ``app.state.webhook_secret`` is set, the request must
        carry ``X-Webhook-Signature: sha256=<hex>`` computed over the raw body
        using the shared secret.  When absent the handler logs a loud warning
        and continues (permissive until OpenClaw gains signing support).
        """
        raw = await request.body()

        # -- HMAC verification -----------------------------------------------
        wh_secret: str = getattr(request.app.state, "webhook_secret", "")
        if wh_secret:
            sig_header = request.headers.get("X-Webhook-Signature", "")
            expected = "sha256=" + _hmac.new(
                wh_secret.encode(), raw, "sha256"
            ).hexdigest()
            if not sig_header or not _hmac.compare_digest(sig_header, expected):
                logger.warning(
                    "webhook_a2a: invalid/missing signature from ip=%s",
                    _client_ip(request),
                )
                return JSONResponse({"error": "invalid_signature"}, status_code=401)
        else:
            logger.warning(
                "webhook_a2a: no webhook secret configured — request from ip=%s accepted "
                "without signature verification. Set OBSCURA_WEBHOOK_SECRET or "
                "~/.obscura/network-gateway-webhook.secret to require signing.",
                _client_ip(request),
            )

        # -- Parse + structural validation -----------------------------------
        try:
            body: dict = _json.loads(raw)
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)

        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid_payload"}, status_code=400)

        if len(body) > 50:
            return JSONResponse({"error": "payload_too_large"}, status_code=400)

        task_id = body.get("task_id") or body.get("id", "?")
        task_type = body.get("type", "push_notification")

        # -- Extract result text from various payload shapes -----------------
        text: str = ""
        if "result" in body:
            result_val = body["result"]
            text = result_val if isinstance(result_val, str) else str(result_val)
        elif "artifacts" in body:
            for art in body.get("artifacts", []):
                if not isinstance(art, dict):
                    continue
                for part in art.get("parts", []):
                    if isinstance(part, dict) and part.get("kind") == "text":
                        part_text = part.get("text", "")
                        if isinstance(part_text, str):
                            text += part_text
        elif "message" in body:
            msg = body.get("message")
            if isinstance(msg, dict):
                for part in msg.get("parts", []):
                    if isinstance(part, dict) and part.get("kind") == "text":
                        part_text = part.get("text", "")
                        if isinstance(part_text, str):
                            text += part_text
        if not text:
            text = f"[{task_type}] task={task_id}"

        # -- Sanitize sender identity ----------------------------------------
        raw_sender = str(body.get("from") or body.get("agent") or "")[:64]
        # Allow alphanumeric + safe punctuation only; fall back to generic label
        sender = raw_sender if _re.fullmatch(r"[\w.\-]+", raw_sender) else "webhook-peer"

        # -- Inject into REPL channel via UDS --------------------------------
        # The gateway runs in a separate process from the REPL; the in-process
        # channel queue is unreachable from here.  Use UDS broadcast instead:
        # discover_peers() finds all live REPL sockets, send_message() delivers
        # the payload to each one, and UDSInbox._on_peer_message (in the REPL
        # process) calls push_channel_message() into the REPL's own queue.
        try:
            from obscura.kairos.uds_messaging import discover_peers, send_message

            peers = discover_peers()
            if peers:
                uds_payload = {
                    "from": sender,
                    "from_session": "webhook",
                    "text": text,
                    "backend": "a2a-webhook",
                }
                delivered = 0
                for _sid in peers:
                    if await send_message(_sid, uds_payload):
                        delivered += 1
                logger.info(
                    "webhook_a2a: injected into %d/%d REPL session(s)",
                    delivered, len(peers),
                )
            else:
                logger.debug("webhook_a2a: no active REPL sessions to inject into")
        except Exception:
            logger.debug("webhook_a2a: UDS inject failed", exc_info=True)

        logger.info(
            "webhook/a2a: received task=%s type=%s from=%s",
            task_id, task_type, sender,
        )
        return JSONResponse({"ok": True, "task_id": task_id})

    logger.info(
        "Network gateway configured: host=%s port=%d backend=%s auth=%s",
        config.host,
        config.port,
        config.agent_backend,
        "enabled" if config.token else "disabled",
    )

    return app


# Module-level app — enables:
#   uvicorn obscura.integrations.network_gateway.app:app --port 18790
app: FastAPI = create_gateway_app()


def main() -> None:
    """Standalone entry point for the network gateway.

    Reads configuration from ObscuraConfig + env overrides and starts
    a blocking uvicorn server.  Equivalent to::

        uvicorn obscura.integrations.network_gateway.app:app --port 18790
    """
    import uvicorn

    cfg = GatewayConfig.from_obscura_config()
    uvicorn.run(create_gateway_app(cfg), host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()

__all__ = ["app", "create_gateway_app", "main"]
