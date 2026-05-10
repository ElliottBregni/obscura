"""obscura.integrations.network_gateway.app — FastAPI application factory.

Exposes Obscura agents over HTTP/WebSocket on port 18790:

* ``POST /v1/chat/completions`` — OpenAI-compatible chat completions
* ``GET  /v1/models``           — list Obscura backends as model objects
* ``WS   /v1/chat/ws``          — bidirectional streaming WebSocket chat
* ``GET  /health``              — unauthenticated health probe

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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from obscura.auth.security_headers import SecurityHeadersMiddleware
from obscura.integrations.network_gateway.chat_completions import (
    router as chat_router,
)
from obscura.integrations.network_gateway.config import GatewayConfig
from obscura.integrations.network_gateway.models import router as models_router
from obscura.integrations.network_gateway.ws import ws_router

logger = logging.getLogger(__name__)


def create_gateway_app(config: GatewayConfig | None = None) -> FastAPI:
    """Build and return the network gateway :class:`~fastapi.FastAPI` application.

    Parameters
    ----------
    config:
        Gateway configuration.  Defaults to :class:`GatewayConfig` with all
        fields at their defaults when ``None`` is passed.

    Returns
    -------
    FastAPI
        Fully configured application ready to be served with uvicorn.

    """
    if config is None:
        config = GatewayConfig()

    app = FastAPI(
        title="Obscura Network Gateway",
        description=(
            "OpenAI-compatible HTTP/WebSocket gateway for Obscura AI agents. "
            "Runs on port 18790."
        ),
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Stash config on app.state so route handlers can access it.
    app.state.gateway_config = config

    # -- Middleware stack (add_middleware wraps in LIFO order) ---------------
    # Desired inbound order: SecurityHeaders → CORS → routes
    # So we register CORS first, then SecurityHeaders last.

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_middleware(SecurityHeadersMiddleware)

    # -- Routers ------------------------------------------------------------

    app.include_router(chat_router)
    app.include_router(models_router)
    app.include_router(ws_router)

    # -- Unauthenticated health probe ---------------------------------------

    @app.get("/health", tags=["health"])
    async def health() -> JSONResponse:
        """Unauthenticated health probe."""
        return JSONResponse(
            content={
                "status": "ok",
                "service": "obscura-network-gateway",
                "port": config.port,  # type: ignore[union-attr]
            },
        )

    logger.info(
        "Network gateway configured: host=%s port=%d",
        config.host,
        config.port,
    )

    return app


# Module-level app — enables:
#   uvicorn obscura.integrations.network_gateway.app:app --port 18790
app: FastAPI = create_gateway_app()

__all__ = ["app", "create_gateway_app"]
