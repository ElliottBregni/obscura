"""obscura.integrations.network_gateway.config — GatewayConfig dataclass.

Token resolution order (first non-empty wins):

1. ``OBSCURA_NETWORK_TOKEN`` env var.
2. ``~/.obscura/network-gateway.token`` file (first non-comment line).
3. Empty string — no auth enforced (useful for fully-trusted private nets).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_TOKEN_FILE = Path.home() / ".obscura" / "network-gateway.token"
_DEFAULT_WEBHOOK_SECRET_FILE = Path.home() / ".obscura" / "network-gateway-webhook.secret"

# Obscura backends advertised by the gateway
KNOWN_BACKENDS: tuple[str, ...] = (
    "claude",
    "copilot",
    "codex",
    "localllm",
)


def _resolve_token() -> str:
    """Return the configured bearer token, or empty string if none."""
    env_val = os.environ.get("OBSCURA_NETWORK_TOKEN", "").strip()
    if env_val:
        logger.debug("Network gateway: loaded token from OBSCURA_NETWORK_TOKEN")
        return env_val

    try:
        lines = _DEFAULT_TOKEN_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""

    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if line:
            logger.debug("Network gateway: loaded token from %s", _DEFAULT_TOKEN_FILE)
            return line

    return ""


def _resolve_webhook_secret() -> str:
    """Return the webhook HMAC secret, or empty string if not configured."""
    env_val = os.environ.get("OBSCURA_WEBHOOK_SECRET", "").strip()
    if env_val:
        return env_val
    try:
        lines = _DEFAULT_WEBHOOK_SECRET_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if line:
            return line
    return ""


@dataclass
class GatewayConfig:
    """Configuration for the Obscura network gateway.

    Parameters
    ----------
    host:
        Interface to bind. Defaults to ``"0.0.0.0"`` (all interfaces).
    port:
        TCP port. Defaults to 18790.
    agent_backend:
        Default Obscura backend used for chat completions when the caller's
        ``model`` field is ``"obscura"`` or blank. One of
        ``"claude"``, ``"copilot"``, ``"codex"``, ``"localllm"``.
    agent_model:
        Optional model override forwarded to the backend (e.g.
        ``"claude-sonnet-4-5"``). Empty string lets the backend choose.
    token:
        Bearer token. Empty string disables auth. If not supplied, resolved
        from ``OBSCURA_NETWORK_TOKEN`` env var or
        ``~/.obscura/network-gateway.token``.
    cors_origins:
        CORS ``allow_origins`` list. Defaults to OpenClaw's localhost origins.
    debug:
        Enable Swagger UI (``/docs``) and ReDoc (``/redoc``). Defaults to
        ``False`` (disabled in production).
    max_request_bytes:
        Maximum request body size in bytes enforced by
        ``RequestSizeLimitMiddleware``. Defaults to 1 MB.
    rate_limit:
        Max requests per 60-second window per client IP. Defaults to 60.
    request_timeout:
        Timeout in seconds for chat completion requests and WebSocket proxy
        calls. Defaults to 120.0.
    ws_ping_interval:
        Interval in seconds between server-side WebSocket keepalive pings.
        Defaults to 30.0.
    session_ttl:
        Idle session expiry in seconds for the WebSocket session store.
        Defaults to 3600.0 (1 hour).
    """

    host: str = "0.0.0.0"
    port: int = 18790
    agent_backend: str = "claude"
    agent_model: str = ""
    token: str = ""
    webhook_secret: str = field(default_factory=_resolve_webhook_secret)
    cors_origins: list[str] = field(
        default_factory=lambda: [
            "http://localhost:18789",
            "http://127.0.0.1:18789",
        ]
    )
    rate_limit: int = 60
    tailscale_enabled: bool = False
    tailscale_url: str = ""  # e.g. https://modernizedai.tail91e620.ts.net
    request_timeout: float = 120.0
    ws_ping_interval: float = 30.0
    session_ttl: float = 3600.0
    debug: bool = False
    max_request_bytes: int = 1 * 1024 * 1024  # 1 MB
    strict_webhook_verification: bool = True
    """Reject webhook requests when their HMAC secret is not configured.
    When False, accepts unverified payloads and logs a warning (permissive mode)."""
    webhook_rate_limit: int = 20
    """Max webhook+channel requests per 60-second window per IP (tighter than main rate limit)."""
    control_plane_rate_limit: int = 10
    """Max control-plane mutation requests (POST/PATCH/DELETE to /channels/configs) per 60-second
    window per IP. Tighter than the general rate limit."""
    # Standalone agent — lightweight direct-chat server on a dedicated port.
    standalone_agent_enabled: bool = False
    standalone_agent_port: int = 18792
    standalone_agent_host: str = "0.0.0.0"
    # Heartbeat subsystem — periodic scheduled agent turn broadcast to WS clients.
    heartbeat_enabled: bool = False
    heartbeat_interval: float = 1800.0  # 30 minutes
    heartbeat_prompt: str = ""          # empty = use default
    heartbeat_target: str = "ws"        # "ws" | "last"

    @classmethod
    def from_obscura_config(cls) -> "GatewayConfig":
        """Build a GatewayConfig from ``ObscuraConfig`` + env overrides."""
        from obscura.core.config import ObscuraConfig

        cfg = ObscuraConfig.load()
        cors_origins: list[str] = [
            "http://localhost:18789",
            "http://127.0.0.1:18789",
        ]
        tailscale_url = cfg.network_gateway_tailscale_url
        if tailscale_url:
            if tailscale_url != "*" and tailscale_url.startswith("https://"):
                cors_origins.append(tailscale_url)
            else:
                logger.warning(
                    "GatewayConfig: ignoring unsafe cors tailscale_url=%r "
                    "(must start with https://)",
                    tailscale_url,
                )
        return cls(
            host=os.environ.get("OBSCURA_GATEWAY_HOST", "0.0.0.0"),
            port=int(os.environ.get("OBSCURA_GATEWAY_PORT", "18790")),
            agent_backend=cfg.default_backend or "claude",
            agent_model="",
            token=_resolve_token(),
            cors_origins=cors_origins,
            rate_limit=cfg.a2a_inbound_rate_limit,
            tailscale_enabled=cfg.network_gateway_tailscale_enabled,
            tailscale_url=tailscale_url,
            request_timeout=cfg.network_gateway_request_timeout,
            ws_ping_interval=cfg.network_gateway_ws_ping_interval,
            session_ttl=cfg.network_gateway_session_ttl,
            debug=os.environ.get("OBSCURA_GATEWAY_DEBUG", "").lower() in ("1", "true"),
            standalone_agent_enabled=cfg.standalone_agent_enabled,
            standalone_agent_port=int(
                os.environ.get(
                    "OBSCURA_STANDALONE_AGENT_PORT",
                    str(cfg.standalone_agent_port),
                )
            ),
            standalone_agent_host=os.environ.get(
                "OBSCURA_STANDALONE_AGENT_HOST",
                cfg.standalone_agent_host,
            ),
            strict_webhook_verification=True,  # always on; no config field yet
            webhook_rate_limit=20,
            control_plane_rate_limit=10,
        )


__all__ = ["GatewayConfig", "KNOWN_BACKENDS"]
