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

import asyncio
import contextlib
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
    ControlPlaneRateLimitMiddleware,
    GatewayBearerAuthMiddleware,
    GatewayRateLimitMiddleware,
    RequestSizeLimitMiddleware,
    WebhookRateLimitMiddleware,
    _client_ip,
)
from obscura.integrations.network_gateway.chat_completions import (
    router as chat_router,
)
from obscura.integrations.network_gateway.config import GatewayConfig
from obscura.integrations.network_gateway.models import router as models_router
from obscura.integrations.network_gateway.sessions import init_session_store
from obscura.integrations.network_gateway.ws import ws_router
from obscura.routes.channels import init_channel_router
from obscura.routes.channels import router as channels_router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)

_webhook_unauth_warned: bool = False


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

    # Start the process-level channel fanout — fans incoming platform messages
    # (WhatsApp, Telegram, etc.) out to all connected WS clients via the
    # ConnectionRegistry broadcast.
    from obscura.integrations.network_gateway.connections import get_registry as _get_registry
    _conn_registry = _get_registry()
    _conn_registry.start_channel_fanout()

    # Heartbeat subsystem — periodic scheduled agent turn broadcast to WS clients.
    _heartbeat = None
    if gateway_config is not None and gateway_config.heartbeat_enabled:
        from obscura.integrations.network_gateway.heartbeat import HeartbeatTask
        _heartbeat = HeartbeatTask(
            _conn_registry,
            interval=gateway_config.heartbeat_interval,
            prompt=gateway_config.heartbeat_prompt or "",
            backend=gateway_config.agent_backend if gateway_config else "claude",
            target=gateway_config.heartbeat_target,
        )
        _heartbeat.start()
    app.state.heartbeat_task = _heartbeat

    # Messaging platform wiring — initialise ChannelRouter so the channels
    # webhook routes (/channels/telegram/webhook, /channels/whatsapp/) work
    # on the main gateway (which Tailscale exposes).  Best-effort; failures
    # are logged and the gateway starts anyway.
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
        import hashlib as _hashlib
        import time as _time_mod

        _platforms = load_messaging_platforms()
        if _platforms:
            _runner = ObscuraAgentRunner(
                backend=gateway_config.agent_backend if gateway_config else "claude",
                tool_registry=None,
            )
            _ch_cfg = ChannelRouterConfig(mode=ChannelMode.CHANNEL_INJECT)
            _ch_router = ChannelRouter(runner=_runner, config=_ch_cfg)
            _loaded = 0
            for _pconf in _platforms:
                if not _pconf.get("enabled", True):
                    continue
                try:
                    _pid = str(_pconf.get("platform", "unknown"))
                    _rec = ChannelConfigRecord.from_dict({
                        "id": _hashlib.md5(_pid.encode()).hexdigest(),
                        "platform": _pid,
                        "label": _pconf.get("label", _pid),
                        "enabled": _pconf.get("enabled", True),
                        "mode": _pconf.get("mode", "channel_inject"),
                        "credentials": _pconf.get("credentials", {}),
                        "contacts": _pconf.get("contacts", []),
                        "router_config": {},
                        "created_at_epoch_s": _time_mod.time(),
                        "updated_at_epoch_s": _time_mod.time(),
                    })
                    await _ch_router.apply_config(_rec)
                    _loaded += 1
                except Exception:
                    logger.warning(
                        "Gateway: failed to apply platform config=%s",
                        _pconf.get("platform"),
                        exc_info=True,
                    )
            init_channel_router(_ch_router)
            logger.info("Gateway: messaging platforms loaded — %d active", _loaded)
    except Exception:
        logger.warning(
            "Gateway: messaging platform wiring failed — channel webhooks will return 503",
            exc_info=True,
        )

    # Tailscale serve — expose gateway to tailnet peers
    _tailscale_active = False
    _tailscale_sa_active = False
    if gateway_config is not None and gateway_config.tailscale_enabled:
        _tailscale_active = await configure_tailscale_serve(gateway_config.port)
        if _tailscale_active:
            ts_url = (
                detect_tailscale_url()
                or gateway_config.tailscale_url
                or "<tailscale-url>"
            )
            logger.info("Gateway also reachable at %s", ts_url)
            # Also expose standalone agent browser UI on its own HTTPS port.
            if gateway_config.standalone_agent_enabled:
                sa_port = gateway_config.standalone_agent_port
                _tailscale_sa_active = await configure_tailscale_serve(
                    sa_port, listen_port=sa_port
                )
                if _tailscale_sa_active:
                    logger.info(
                        "Standalone agent chat UI also reachable at %s:%d/",
                        ts_url,
                        sa_port,
                    )

    # Standalone agent — spin up on a dedicated port if enabled.
    _sa_task: asyncio.Task[None] | None = None
    if gateway_config is not None and gateway_config.standalone_agent_enabled:
        import asyncio as _asyncio

        import uvicorn as _uvicorn

        from obscura.integrations.network_gateway.standalone_agent import (
            create_standalone_agent_app,
        )

        standalone_app = create_standalone_agent_app(gateway_config)
        _sa_config = _uvicorn.Config(
            standalone_app,
            host=gateway_config.standalone_agent_host,
            port=gateway_config.standalone_agent_port,
            log_level="warning",
        )
        _sa_server = _uvicorn.Server(_sa_config)
        _sa_task = _asyncio.create_task(_sa_server.serve())
        logger.info(
            "Standalone agent listening on %s:%d",
            gateway_config.standalone_agent_host,
            gateway_config.standalone_agent_port,
        )

    try:
        yield
    finally:
        if _heartbeat is not None:
            _heartbeat.stop()
        _conn_registry.stop_channel_fanout()
        if _sa_task is not None:
            _sa_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _sa_task
        if _tailscale_active and gateway_config is not None:
            await remove_tailscale_serve(gateway_config.port)
        if _tailscale_sa_active and gateway_config is not None:
            sa_port = gateway_config.standalone_agent_port
            await remove_tailscale_serve(sa_port, listen_port=sa_port)
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
    app.state.strict_webhook_verification = config.strict_webhook_verification

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

    # Tighter rate limit for webhook + channel paths
    app.add_middleware(WebhookRateLimitMiddleware, max_requests=config.webhook_rate_limit)

    # Control-plane rate limit for config mutation endpoints
    app.add_middleware(ControlPlaneRateLimitMiddleware, max_requests=config.control_plane_rate_limit)

    # Security headers
    app.add_middleware(SecurityHeadersMiddleware)

    # Request body size limit (outermost — catches oversized requests first)
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=config.max_request_bytes)

    # -- Unauthenticated health probe ---------------------------------------

    resolved_config = config

    @app.get("/health", tags=["health"])
    async def health(request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """Unauthenticated liveness probe."""
        result: dict[str, Any] = {
            "status": "ok",
            "service": "obscura-network-gateway",
            "port": resolved_config.port,
        }

        # Heartbeat status
        hb = getattr(request.app.state, "heartbeat_task", None)
        if hb is not None:
            result["heartbeat"] = {
                "enabled": True,
                "interval": hb._interval,
                "last_run": hb.last_run,
                "target": hb._target,
            }

        # Per-channel connectivity status
        channels: dict[str, Any] = {}
        try:
            from obscura.routes.channels import _channel_router as _ch_router_ref
            if _ch_router_ref is not None:
                adapters = getattr(_ch_router_ref, "_adapters", {})
                for platform_id, adapter in adapters.items():
                    try:
                        probe = await asyncio.wait_for(adapter.health_check(), timeout=3.0)
                    except asyncio.TimeoutError:
                        probe = {"status": "timeout"}
                    except AttributeError:
                        probe = {"status": "unknown"}  # adapter has no health_check
                    except Exception as exc:
                        probe = {"status": "error", "detail": str(exc)[:120]}
                    channels[platform_id] = probe
        except Exception:
            channels = {}
        result["channels"] = channels

        return result

    # -- Connected peers endpoint ------------------------------------------

    from obscura.integrations.network_gateway.connections import get_registry as _get_peers_registry

    @app.get("/v1/peers", tags=["peers"])
    async def list_peers() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """List all connected WS clients and active REPL sessions.

        Returns a JSON object with:
        - ``ws_clients``: connected WebSocket clients (conn_id, endpoint, remote)
        - ``repl_sessions``: active REPL sessions reachable via UDS
        - ``total``: total count of all connected nodes
        """
        registry = _get_peers_registry()
        ws_clients = registry.snapshot()

        repl_sessions: list[dict[str, str]] = []
        try:
            from obscura.kairos.uds_messaging import discover_peers
            repl_sessions = [{"session_id": sid, "type": "repl"} for sid in discover_peers()]
        except Exception:
            pass

        return {
            "ws_clients": ws_clients,
            "repl_sessions": repl_sessions,
            "total": len(ws_clients) + len(repl_sessions),
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

    # -- Messaging channel webhooks + config CRUD --------------------------
    # Webhook paths (/channels/telegram/webhook, /channels/whatsapp/) are
    # auth-exempt (listed in _AUTH_EXEMPT_PREFIXES in auth.py) and use their
    # own HMAC/token verification.  Config CRUD endpoints require bearer auth.
    # Mounting here makes them reachable through Tailscale (:18790) so external
    # platforms can POST to https://<machine>.ts.net/channels/…/webhook.
    app.include_router(channels_router)

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
        strict: bool = getattr(request.app.state, "strict_webhook_verification", False)
        if not wh_secret and strict:
            return JSONResponse({"error": "webhook_not_configured"}, status_code=503)
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
            global _webhook_unauth_warned
            if not _webhook_unauth_warned:
                _webhook_unauth_warned = True
                logger.warning(
                    "webhook_a2a: no webhook secret configured — requests accepted without "
                    "signature verification. Set OBSCURA_WEBHOOK_SECRET or "
                    "~/.obscura/network-gateway-webhook.secret to enable signing."
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
