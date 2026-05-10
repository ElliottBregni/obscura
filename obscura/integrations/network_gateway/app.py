"""obscura.integrations.network_gateway.app — FastAPI gateway factory.

Exposes Obscura agents over HTTP on port 18790 with:

* ``POST /v1/chat/completions`` — OpenAI-compatible chat completions
* ``GET  /v1/models``           — list Obscura backends as model objects
* ``WS   /v1/chat/ws``          — bidirectional streaming WebSocket chat
* A2A routers at ``/a2a/``      — full A2A protocol (JSON-RPC, REST, SSE)
* ``GET  /health``              — unauthenticated liveness probe
* ``GET  /.well-known/agent.json`` — A2A discovery (always public)

Middleware stack (outermost → innermost, request direction):

    SecurityHeaders → GatewayRateLimit → GatewayBearerAuth → CORS → routes

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

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from obscura.auth.security_headers import SecurityHeadersMiddleware
from obscura.integrations.network_gateway.auth import (
    GatewayBearerAuthMiddleware,
    GatewayRateLimitMiddleware,
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
        1. ``SecurityHeadersMiddleware``
        2. ``GatewayRateLimitMiddleware``
           (60 req/min per IP; exempt: ``/health``, ``/.well-known/``)
        3. ``GatewayBearerAuthMiddleware``
           (when token configured; exempt: ``/health``, ``/.well-known/``)
        4. CORS

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
        agent_model=config.agent_model or config.agent_backend,
    )

    app = FastAPI(
        title="Obscura Network Gateway",
        description=(
            "OpenAI-compatible + A2A gateway for Obscura AI agents. Runs on port 18790."
        ),
        version="0.7.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=_lifespan,
    )

    # Stash config + A2A server on state so route handlers can access them.
    app.state.gateway_config = config
    app.state.a2a_server = a2a_server

    # -- Middleware stack (add_middleware wraps in LIFO order) ---------------
    # Desired inbound order:
    #   SecurityHeaders → GatewayRateLimit → GatewayBearerAuth → CORS → routes
    # Register innermost first (CORS), outermost last (SecurityHeaders).

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Bearer auth (token from GatewayConfig.token; empty = no auth)
    app.add_middleware(GatewayBearerAuthMiddleware, token=config.token)

    # Per-IP sliding-window rate limiter
    app.add_middleware(GatewayRateLimitMiddleware, max_requests=config.rate_limit)

    # Security headers (outermost)
    app.add_middleware(SecurityHeadersMiddleware)

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
