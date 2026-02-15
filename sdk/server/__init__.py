"""
sdk.server -- FastAPI HTTP API wrapping the ObscuraClient SDK.

Endpoints are defined in ``sdk.routes.*`` and registered via
``app.include_router()``.  This module provides the app factory,
middleware stack, and lifespan management.

Start the server via::

    obscura-sdk serve [--host 0.0.0.0] [--port 8080] [--reload]
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from sdk.auth.middleware import JWKSCache, JWTAuthMiddleware
from sdk.config import ObscuraConfig
from sdk.deps import ClientFactory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup / shutdown lifecycle for the server."""
    config: ObscuraConfig = app.state.config
    logger.info(
        "Obscura SDK server starting (host=%s port=%d)", config.host, config.port
    )

    # Initialize telemetry (traces, metrics, structured logging)
    try:
        from sdk.telemetry import init_telemetry

        init_telemetry(config)
        logger.info("Telemetry initialized (otel_enabled=%s)", config.otel_enabled)
    except Exception:
        logger.warning(
            "Could not initialize telemetry; continuing without observability"
        )

    # Warm the JWKS cache
    if config.auth_enabled:
        jwks: JWKSCache = app.state.jwks_cache
        try:
            await jwks.refresh()
            logger.info("JWKS cache warmed (%d keys)", len(jwks.keys))
        except Exception:
            logger.warning("Could not pre-fetch JWKS; will retry on first request")

    # Initialize heartbeat monitor
    try:
        from sdk.heartbeat import get_default_monitor

        monitor = get_default_monitor()
        await monitor.start()
        app.state._heartbeat_monitor = monitor
        logger.info("Heartbeat monitor started")
    except Exception:
        logger.warning(
            "Could not initialize heartbeat monitor; continuing without health monitoring"
        )
        app.state._heartbeat_monitor = None

    yield

    # Cleanup heartbeat monitor
    if app.state._heartbeat_monitor:
        try:
            await app.state._heartbeat_monitor.stop()
            logger.info("Heartbeat monitor stopped")
        except Exception:
            pass

    logger.info("Obscura SDK server shutting down")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(config: ObscuraConfig | None = None) -> FastAPI:
    """Build and return the FastAPI application."""
    from dotenv import load_dotenv

    load_dotenv()

    if config is None:
        config = ObscuraConfig.from_env()

    app = FastAPI(
        title="Obscura SDK API",
        version="0.2.0",
        lifespan=lifespan,
    )

    # Stash shared state
    app.state.config = config
    app.state.client_factory = ClientFactory(config)
    app.state._heartbeat_monitor = None
    app.state._health_ws_clients = []

    # -- middleware (order matters: innermost first) ------------------------

    if config.otel_enabled:
        try:
            from sdk.telemetry.middleware import ObscuraTelemetryMiddleware

            app.add_middleware(ObscuraTelemetryMiddleware)  # pyright: ignore[reportArgumentType]
        except ImportError:
            logger.debug("Telemetry middleware not available; skipping")

    if config.auth_enabled:
        jwks_cache = JWKSCache(config.auth_jwks_uri)
        app.state.jwks_cache = jwks_cache
        app.add_middleware(
            JWTAuthMiddleware,
            jwks_cache=jwks_cache,
            issuer=config.auth_issuer,
            audience=config.auth_audience,
        )

    cors_origins = os.environ.get(
        "OBSCURA_CORS_ORIGINS",
        "http://localhost:5173,http://localhost:8080,http://localhost:3000",
    ).split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=r"http://localhost:\d+",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- global exception handler ------------------------------------------

    async def _handle_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.error(f"Unhandled error: {exc}")
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc)},
        )

    app.add_exception_handler(Exception, _handle_exception)

    # -- MCP routes --------------------------------------------------------

    try:
        from sdk.mcp.server import ObscuraMCPServer, create_mcp_router

        mcp_server = ObscuraMCPServer()
        mcp_router = create_mcp_router(mcp_server)
        app.include_router(mcp_router)
        logger.info("MCP router added")
    except Exception as e:
        logger.warning(f"Could not initialize MCP router: {e}")

    # -- API routes --------------------------------------------------------

    from sdk.routes import all_routers

    for router in all_routers:
        app.include_router(router)

    return app
