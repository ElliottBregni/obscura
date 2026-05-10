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
        CORS ``allow_origins`` list. Defaults to ``["*"]``.
    rate_limit:
        Max requests per 60-second window per client IP. Defaults to 60.
    """

    host: str = "0.0.0.0"
    port: int = 18790
    agent_backend: str = "claude"
    agent_model: str = ""
    token: str = ""
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    rate_limit: int = 60

    @classmethod
    def from_obscura_config(cls) -> "GatewayConfig":
        """Build a GatewayConfig from ``ObscuraConfig`` + env overrides."""
        from obscura.core.config import ObscuraConfig

        cfg = ObscuraConfig.load()
        return cls(
            host=os.environ.get("OBSCURA_GATEWAY_HOST", "0.0.0.0"),
            port=int(os.environ.get("OBSCURA_GATEWAY_PORT", "18790")),
            agent_backend=cfg.default_backend or "claude",
            agent_model="",
            token=_resolve_token(),
            cors_origins=["*"],
            rate_limit=cfg.a2a_inbound_rate_limit,
        )


__all__ = ["GatewayConfig", "KNOWN_BACKENDS"]
