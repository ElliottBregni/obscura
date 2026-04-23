"""obscura.server -- FastAPI HTTP API wrapping the ObscuraClient SDK.

Endpoints are defined in ``obscura.routes.*`` and registered via
``app.include_router()``.  This module provides the app factory,
middleware stack, and lifespan management.

Start the server via::

    obscura-sdk serve [--host 0.0.0.0] [--port 8080] [--reload]
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from obscura.auth.middleware import APIKeyAuthMiddleware
from obscura.core.config import ObscuraConfig
from obscura.deps import ClientFactory

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Startup / shutdown lifecycle for the server."""
    config: ObscuraConfig = app.state.config
    logger.info(
        "Obscura SDK server starting (host=%s port=%d)",
        config.host,
        config.port,
    )

    # Initialize telemetry (traces, metrics, structured logging)
    try:
        from obscura.telemetry import init_telemetry

        init_telemetry(config)
        logger.info("Telemetry initialized (otel_enabled=%s)", config.otel_enabled)
    except Exception:
        logger.warning(
            "Could not initialize telemetry; continuing without observability",
        )

    # Initialize A2A server
    if config.a2a_enabled and hasattr(app.state, "a2a_server"):
        try:
            await app.state.a2a_server.startup()
            logger.info("A2A server started")
        except Exception:
            logger.warning("Could not start A2A server; continuing without A2A")

    # Initialize heartbeat monitor
    try:
        from obscura.heartbeat import get_default_monitor

        monitor = get_default_monitor()
        await monitor.start()
        app.state._heartbeat_monitor = monitor
        logger.info("Heartbeat monitor started")
    except Exception:
        logger.warning(
            "Could not initialize heartbeat monitor; continuing without health monitoring",
        )
        app.state._heartbeat_monitor = None

    # Load persisted agent templates into memory
    try:
        from obscura.routes.template_store import load_persisted_templates, put

        persisted = load_persisted_templates()
        for tid, tdata in persisted.items():
            put(tid, tdata)
        if persisted:
            logger.info("Loaded %d persisted agent templates", len(persisted))
    except Exception:
        logger.warning(
            "Could not load persisted agent templates; starting with empty store",
        )

    # Initialize channel router (Telegram / WhatsApp webhooks)
    # Activates only when platform credentials are present in env.
    try:
        _has_telegram = bool(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        _has_whatsapp = bool(
            os.environ.get("TWILIO_ACCOUNT_SID", "")
            and os.environ.get("TWILIO_AUTH_TOKEN", "")
        )
        if _has_telegram or _has_whatsapp:
            from obscura.integrations.messaging.factory import build_channel_router
            from obscura.routes.channels import init_channel_router

            channel_router = await build_channel_router()
            init_channel_router(channel_router)
            logger.info(
                "Channel router initialized (telegram=%s, whatsapp=%s)",
                _has_telegram,
                _has_whatsapp,
            )
        else:
            logger.debug(
                "Channel router skipped — set TELEGRAM_BOT_TOKEN or "
                "TWILIO_ACCOUNT_SID+TWILIO_AUTH_TOKEN to enable"
            )
    except Exception:
        logger.warning(
            "Could not initialize channel router; webhook endpoints will return 503",
            exc_info=True,
        )

    # Apply any spec-driven channel configs persisted in the DB.
    # This runs regardless of whether env-var credentials were found — DB configs
    # can register additional channels (or override env-var channels) at startup.
    try:
        from obscura.integrations.messaging.store import ChannelConfigStore
        from obscura.routes.channels import _get_router as _get_channel_router

        _db_config_store = ChannelConfigStore()
        _enabled_configs = _db_config_store.list_all(enabled_only=True)
        if _enabled_configs:
            try:
                _live_router = _get_channel_router()
            except Exception:
                # Router not yet initialized (no env creds); build a minimal one
                from obscura.integrations.messaging.factory import build_channel_router
                from obscura.routes.channels import init_channel_router

                _live_router = await build_channel_router()
                init_channel_router(_live_router)

            _applied = 0
            for _cfg in _enabled_configs:
                try:
                    await _live_router.apply_config(_cfg)
                    _applied += 1
                except Exception:
                    logger.warning(
                        "Could not apply DB channel config id=%s platform=%s; skipping",
                        _cfg.id,
                        _cfg.platform,
                        exc_info=True,
                    )
            if _applied:
                logger.info(
                    "Applied %d spec-driven channel config(s) from DB", _applied
                )
    except Exception:
        logger.warning(
            "Could not load spec-driven channel configs from DB",
            exc_info=True,
        )

    yield

    # Cleanup A2A server
    if hasattr(app.state, "a2a_server"):
        try:
            await app.state.a2a_server.shutdown()
            logger.info("A2A server stopped")
        except Exception:
            pass

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

    # Refuse to start in the one combination that silently exposes a server
    # without authentication. See ObscuraConfig.validate_deployment_safety.
    config.validate_deployment_safety()

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
            from obscura.telemetry.middleware import ObscuraTelemetryMiddleware

            app.add_middleware(ObscuraTelemetryMiddleware)
        except ImportError:
            logger.debug("Telemetry middleware not available; skipping")

    # Rate limiting (after auth so request.state.user is populated)
    from obscura.auth.rate_limit_middleware import RateLimitMiddleware
    from obscura.core.rate_limiter import RateLimiter

    rate_limiter = RateLimiter(
        default_rpm=config.rate_limit_rpm,
        default_concurrent=config.rate_limit_concurrent,
    )
    app.state.rate_limiter = rate_limiter
    app.add_middleware(RateLimitMiddleware, limiter=rate_limiter)

    if config.auth_enabled:
        app.add_middleware(APIKeyAuthMiddleware)
    else:
        bind_all = config.host in ("0.0.0.0", "::")
        if bind_all:
            logger.warning(
                "Authentication is DISABLED and server binds to %s — "
                "all API endpoints are publicly accessible. "
                "Set OBSCURA_AUTH_ENABLED=true for production.",
                config.host,
            )
        else:
            logger.warning(
                "Authentication is DISABLED. "
                "Set OBSCURA_AUTH_ENABLED=true for production.",
            )

    cors_origins = os.environ.get(
        "OBSCURA_CORS_ORIGINS",
        "http://localhost:5173,http://localhost:8080,http://localhost:3000",
    ).split(",")
    cors_regex = os.environ.get(
        "OBSCURA_CORS_ORIGIN_REGEX",
        r"http://localhost:(3000|5173|8080|8081)",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=cors_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- global exception handler ------------------------------------------

    async def _handle_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    app.add_exception_handler(Exception, _handle_exception)

    # -- MCP routes --------------------------------------------------------

    try:
        from obscura.integrations.mcp.server import ObscuraMCPServer, create_mcp_router

        mcp_server = ObscuraMCPServer()
        mcp_router = create_mcp_router(mcp_server)
        app.include_router(mcp_router)
        logger.info("MCP router added")
    except Exception as e:
        logger.warning(f"Could not initialize MCP router: {e}")

    # -- A2A routes --------------------------------------------------------

    if config.a2a_enabled:
        try:
            from obscura.integrations.a2a.server import ObscuraA2AServer
            from obscura.integrations.a2a.transports import (
                create_jsonrpc_router,
                create_rest_router,
                create_sse_router,
                create_wellknown_router,
            )

            # Choose store backend
            if config.a2a_redis_url:
                from obscura.integrations.a2a.store import RedisTaskStore

                a2a_store = RedisTaskStore(
                    config.a2a_redis_url,
                    task_ttl=config.a2a_task_ttl,
                )
            else:
                from obscura.integrations.a2a.store import InMemoryTaskStore

                a2a_store = InMemoryTaskStore()

            a2a_server = ObscuraA2AServer(
                store=a2a_store,
                name=config.a2a_agent_name,
                url=f"http://{config.host}:{config.port}",
                description=config.a2a_agent_description,
            )
            app.state.a2a_server = a2a_server

            # Mount transport routers
            svc = a2a_server.service
            app.include_router(create_jsonrpc_router(svc))
            app.include_router(create_rest_router(svc))
            app.include_router(create_sse_router(svc))
            app.include_router(create_wellknown_router(svc))
            logger.info("A2A routers added (JSON-RPC, REST, SSE, well-known)")
        except Exception as e:
            logger.warning(f"Could not initialize A2A: {e}")

    # -- API routes --------------------------------------------------------

    from obscura.routes import all_routers

    for router in all_routers:
        app.include_router(router)

    return app
