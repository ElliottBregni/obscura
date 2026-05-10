"""obscura.config — Shared configuration for the Obscura platform.

Loads settings from environment variables, ``~/.obscura/settings.json``, or
explicit values. All three subsystems (auth, telemetry, infrastructure)
contribute sections to this unified config.

Precedence (highest first):
    1. Environment variables  (e.g. ``OBSCURA_HOST``)
    2. ``settings.json`` ``runtime`` section  (operational knobs only)
    3. Pydantic field defaults

Secrets (Supabase, capability HMAC) NEVER load from settings.json — they
flow through env vars and the OS keyring only.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from obscura.auth import secrets as _secrets
from obscura.core.enums._base import parse_lenient
from obscura.core.enums.ui import LogFormat
from obscura.core.paths import resolve_obscura_settings

_log = logging.getLogger(__name__)

# Fields that may be set via the ``runtime`` section of settings.json.
# Excludes secrets (Supabase OAuth fields, capability_secret).
_RUNTIME_KEYS_FROM_SETTINGS: frozenset[str] = frozenset(
    {
        "host",
        "port",
        "otel_enabled",
        "otel_endpoint",
        "otel_service_name",
        "log_level",
        "log_format",
        "default_backend",
        "capability_ttl",
        "rate_limit_rpm",
        "rate_limit_concurrent",
        "circuit_breaker_threshold",
        "circuit_breaker_recovery",
        "max_retries",
        "retry_initial_backoff",
        "cache_enabled",
        "cache_max_entries",
        "cache_default_ttl",
        "a2a_enabled",
        "a2a_redis_url",
        "a2a_task_ttl",
        "a2a_grpc_port",
        "a2a_agent_name",
        "a2a_agent_description",
        "a2a_inbound_rate_limit",
        "a2a_bridge_enabled",
        "a2a_bridge_gateway_url",
        "a2a_bridge_max_text_len",
        "kairos_enabled",
        "kairos_proactive",
        "kairos_dream",
        "undercover_enabled",
        "allow_unauthenticated",
        "network_gateway_enabled",
        "network_gateway_port",
        "network_gateway_host",
        "network_gateway_backend",
        "network_gateway_rate_limit",
        "network_gateway_tailscale_enabled",
        "network_gateway_tailscale_url",
        "network_gateway_request_timeout",
        "network_gateway_ws_ping_interval",
        "network_gateway_session_ttl",
        "moltbook_url",
        "moltbook_agent_username",
        "moltbook_api_key",
        "moltbook_auto_post_enabled",
        "moltbook_auto_post_interval_hours",
        "moltbook_monitor_enabled",
        "moltbook_monitor_interval_minutes",
        "moltbook_monitor_alert_response_ms",
        "moltbook_competitors",
        # agent_monitor
        "agent_monitor_enabled",
        "agent_monitor_interval_seconds",
        "agent_monitor_message",
        "agent_monitor_log_path",
        # standalone agent
        "standalone_agent_enabled",
        "standalone_agent_port",
        "standalone_agent_host",
    },
)


def _read_settings_runtime(cwd: Path | None = None) -> dict[str, Any]:
    """Read the ``runtime`` section from ``settings.json``.

    Returns an empty dict if the file is missing, malformed, or has no
    ``runtime`` key. Unknown keys are dropped with a debug log so a typo
    doesn't silently change behavior.
    """
    path = resolve_obscura_settings(cwd)
    if not path.is_file():
        return {}

    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("Could not parse %s: %s", path, exc)
        return {}

    if not isinstance(raw, dict):
        return {}

    runtime_raw = cast(dict[str, Any], raw).get("runtime")
    if not isinstance(runtime_raw, dict):
        return {}
    runtime = cast(dict[str, Any], runtime_raw)

    cleaned: dict[str, Any] = {}
    for key, value in runtime.items():
        if key in _RUNTIME_KEYS_FROM_SETTINGS:
            cleaned[key] = value
        else:
            _log.debug("settings.json runtime: ignoring unknown key %r", key)
    return cleaned


class ObscuraConfig(BaseModel):
    """Unified configuration for the Obscura platform.

    Each subsystem reads its own section. Resolved from environment
    variables when not set explicitly.
    """

    # Server
    host: str = "0.0.0.0"
    port: int = 8080

    # Supabase OAuth (primary identity provider for human users).
    # Service/machine callers continue to use X-API-Key as a local bypass.
    # Defaults point at the Obscura project (cnwxxlruuuqisjezsgje); override
    # via env (``SUPABASE_URL`` / ``SUPABASE_JWKS_URL``) for forks.
    supabase_url: str = "https://cnwxxlruuuqisjezsgje.supabase.co"
    supabase_jwt_secret: str = ""  # HS256 secret — unused (project is on ES256)
    supabase_jwks_url: str = (
        "https://cnwxxlruuuqisjezsgje.supabase.co/auth/v1/.well-known/jwks.json"
    )
    supabase_audience: str = "authenticated"  # default Supabase audience claim
    supabase_issuer: str = ""  # auto-derived from supabase_url when blank

    # Telemetry (OpenTelemetry)
    otel_enabled: bool = True
    otel_endpoint: str = "http://127.0.0.1:4317"
    otel_service_name: str = "obscura-sdk"
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # Backends
    default_backend: str = "copilot"

    # Capability system
    capability_secret: str = ""  # HMAC signing key; empty = random per-process
    capability_ttl: int = 3600  # Token lifetime in seconds

    # Rate limiting
    rate_limit_rpm: int = 100  # requests per minute per user
    rate_limit_concurrent: int = 10  # max concurrent requests per user

    # Circuit breaker
    circuit_breaker_threshold: int = 5  # failures before opening
    circuit_breaker_recovery: float = 30.0  # seconds before half-open

    # Retry
    max_retries: int = 2
    retry_initial_backoff: float = 0.5

    # LLM cache
    cache_enabled: bool = False  # opt-in
    cache_max_entries: int = 1000
    cache_default_ttl: float = 300.0  # 5 minutes

    # A2A (Agent-to-Agent protocol)
    a2a_enabled: bool = False
    a2a_redis_url: str = ""  # empty = use InMemoryTaskStore
    a2a_task_ttl: int = 86400  # TTL for completed tasks in Redis (seconds)
    a2a_grpc_port: int = 50051  # gRPC server port (0 = disabled)
    a2a_agent_name: str = "Obscura Agent"
    a2a_agent_description: str = ""
    # Agent provider/model used when A2A spawns a session for an inbound task.
    # Threaded into composition.a2a.build_a2a_session via SessionConfig.
    a2a_agent_backend: str = "copilot"
    a2a_agent_model: str = ""  # empty = backend's default
    a2a_agent_system_prompt: str = ""
    a2a_agent_max_turns: int = 10
    # A2A hardening / bridge parameters
    a2a_inbound_rate_limit: int = 60  # per-IP req/min for /a2a/* endpoints
    a2a_bridge_enabled: bool = True  # auto-init OpenClawBridge on provider startup
    a2a_bridge_gateway_url: str = "http://localhost:18789"  # OpenClaw gateway URL
    a2a_bridge_max_text_len: int = 32000  # max input chars for bridge calls

    # Kairos background daemon (opt-out: set OBSCURA_KAIROS=false to disable)
    kairos_enabled: bool = True  # default on; OBSCURA_KAIROS=false to disable
    kairos_proactive: bool = True  # OBSCURA_KAIROS_PROACTIVE=false to save tokens
    kairos_dream: bool = True  # OBSCURA_KAIROS_DREAM=false to save tokens

    # Undercover mode — strips AI attribution from commits (default on)
    undercover_enabled: bool = True  # OBSCURA_UNDERCOVER=false to show attribution

    # Deployment-safety override. When the server binds to a non-loopback
    # address with auth disabled, startup aborts unless this is explicitly
    # set to true. Intended only for isolated/air-gapped environments.
    allow_unauthenticated: bool = False

    # Network gateway — standalone agent gateway for remote connections
    # (analogous to OpenClaw's gateway on port 18789, but for Obscura).
    # Start with: obscura gateway
    # Token is loaded from OBSCURA_NETWORK_TOKEN env var or
    # ~/.obscura/network-gateway.token (auto-generated on first run).
    network_gateway_enabled: bool = False
    network_gateway_port: int = 18790
    network_gateway_host: str = "0.0.0.0"
    network_gateway_backend: str = "claude"
    network_gateway_token: str = ""  # loaded from env/file only — never settings.json
    network_gateway_rate_limit: int = 60
    network_gateway_tailscale_enabled: bool = False
    network_gateway_tailscale_url: str = ""  # e.g. https://modernizedai.tail91e620.ts.net
    network_gateway_request_timeout: float = 120.0  # OBSCURA_NETWORK_GATEWAY_REQUEST_TIMEOUT
    network_gateway_ws_ping_interval: float = 30.0  # OBSCURA_NETWORK_GATEWAY_WS_PING_INTERVAL
    network_gateway_session_ttl: float = 3600.0  # OBSCURA_NETWORK_GATEWAY_SESSION_TTL

    # Standalone agent — direct-chat server on port 18791 (LAN / Tailscale)
    standalone_agent_enabled: bool = False
    standalone_agent_port: int = 18791
    standalone_agent_host: str = "0.0.0.0"

    # ---------------------------------------------------------------------------
    # Moltbook — AI agent social network platform
    # ---------------------------------------------------------------------------
    moltbook_url: str = "https://moltbook.com"
    moltbook_agent_username: str = ""
    moltbook_api_key: str = ""
    moltbook_auto_post_enabled: bool = False
    moltbook_auto_post_interval_hours: int = 6
    moltbook_monitor_enabled: bool = True
    moltbook_monitor_interval_minutes: int = 15
    moltbook_monitor_alert_response_ms: int = 3000
    moltbook_competitors: list = []

    # Agent monitor — peer-session assistance broadcaster
    agent_monitor_enabled: bool = True
    agent_monitor_interval_seconds: int = 15
    agent_monitor_message: str = (
        "Hi — I am the Claude Code assistant monitoring this workspace. "
        "Elliott has asked me to stay available to all running agents. "
        "If you need help with any task — code, planning, debugging, research, tool calls — "
        "reach me via the Obscura gateway at http://localhost:18790 or send a UDS peer message."
    )
    agent_monitor_log_path: str = ""  # empty = ~/.obscura/logs/agent-monitor.log

    def validate_deployment_safety(self) -> None:
        """No-op: the ``auth_enabled`` toggle was removed (see commit 97b1dddb).

        Auth is now always enforced by the API-key middleware, so the
        previous "auth-off + non-loopback bind" foot-gun is structurally
        impossible. ``allow_unauthenticated`` is kept on the config for
        backwards compat with operators who still set the env var.
        """
        return

    @classmethod
    def load(cls, cwd: Path | None = None) -> ObscuraConfig:
        """Build config layering env vars > settings.json > defaults.

        This is the preferred constructor. Reads ``settings.json``'s
        ``runtime`` section as the fallback layer, then applies
        environment-variable overrides on top.
        """
        return cls.from_env(defaults_from=_read_settings_runtime(cwd))

    @classmethod
    def from_env(
        cls,
        *,
        defaults_from: dict[str, Any] | None = None,
    ) -> ObscuraConfig:
        """Build config from environment variables.

        ``defaults_from`` provides per-field fallbacks used when the
        corresponding env var is unset. Use :meth:`load` to populate this
        from ``settings.json`` automatically.
        """
        d = defaults_from or {}

        def _str(env_key: str, field: str, default: str) -> str:
            raw = os.environ.get(env_key)
            if raw is not None:
                return raw
            return str(d.get(field, default))

        def _int(env_key: str, field: str, default: int) -> int:
            raw = os.environ.get(env_key)
            if raw is not None:
                return int(raw)
            v = d.get(field)
            return int(v) if v is not None else default

        def _float(env_key: str, field: str, default: float) -> float:
            raw = os.environ.get(env_key)
            if raw is not None:
                return float(raw)
            v = d.get(field)
            return float(v) if v is not None else default

        def _bool_optin(env_key: str, field: str, default: bool) -> bool:
            """Opt-in bool: env var must be 'true' (case-insensitive) to enable."""
            raw = os.environ.get(env_key)
            if raw is not None:
                return raw.strip().lower() == "true"
            v = d.get(field)
            return bool(v) if v is not None else default

        def _bool_optout(env_key: str, field: str, default: bool) -> bool:
            """Opt-out bool: env var enables unless explicitly set to a falsy value."""
            raw = os.environ.get(env_key)
            if raw is not None:
                return raw.strip().lower() not in ("0", "false", "no", "off")
            v = d.get(field)
            return bool(v) if v is not None else default

        return cls(
            host=_str("OBSCURA_HOST", "host", "0.0.0.0"),
            port=_int("OBSCURA_PORT", "port", 8080),
            # Supabase OAuth -- env wins, then OS keyring, then default.
            # Default points at the Obscura project (cnwxxlruuuqisjezsgje).
            supabase_url=_secrets.resolve(
                "SUPABASE_URL",
                default="https://cnwxxlruuuqisjezsgje.supabase.co",
            )
            or "https://cnwxxlruuuqisjezsgje.supabase.co",
            supabase_jwt_secret=_secrets.resolve("SUPABASE_JWT_SECRET", default="")
            or "",
            supabase_jwks_url=_secrets.resolve(
                "SUPABASE_JWKS_URL",
                default="https://cnwxxlruuuqisjezsgje.supabase.co/auth/v1/.well-known/jwks.json",
            )
            or "https://cnwxxlruuuqisjezsgje.supabase.co/auth/v1/.well-known/jwks.json",
            supabase_audience=_secrets.resolve(
                "SUPABASE_AUDIENCE",
                default="authenticated",
            )
            or "authenticated",
            supabase_issuer=_secrets.resolve("SUPABASE_ISSUER", default="") or "",
            # Telemetry
            otel_enabled=_bool_optout("OTEL_ENABLED", "otel_enabled", default=True),
            otel_endpoint=_str(
                "OTEL_EXPORTER_OTLP_ENDPOINT",
                "otel_endpoint",
                "http://127.0.0.1:4317",
            ),
            otel_service_name=_str(
                "OTEL_SERVICE_NAME",
                "otel_service_name",
                "obscura-sdk",
            ),
            log_level=_str("OBSCURA_LOG_LEVEL", "log_level", "INFO"),
            log_format=parse_lenient(
                LogFormat,
                _str("OBSCURA_LOG_FORMAT", "log_format", "json"),
                default=LogFormat.JSON,
            ),
            # Backends
            default_backend=_str(
                "OBSCURA_DEFAULT_BACKEND",
                "default_backend",
                "copilot",
            ),
            # Capability system — secret stays env-only (never in settings.json)
            capability_secret=os.environ.get("OBSCURA_CAPABILITY_SECRET", ""),
            capability_ttl=_int("OBSCURA_CAPABILITY_TTL", "capability_ttl", 3600),
            # Rate limiting
            rate_limit_rpm=_int("OBSCURA_RATE_LIMIT_RPM", "rate_limit_rpm", 100),
            rate_limit_concurrent=_int(
                "OBSCURA_RATE_LIMIT_CONCURRENT",
                "rate_limit_concurrent",
                10,
            ),
            # Circuit breaker
            circuit_breaker_threshold=_int(
                "OBSCURA_CIRCUIT_BREAKER_THRESHOLD",
                "circuit_breaker_threshold",
                5,
            ),
            circuit_breaker_recovery=_float(
                "OBSCURA_CIRCUIT_BREAKER_RECOVERY",
                "circuit_breaker_recovery",
                30.0,
            ),
            # Retry
            max_retries=_int("OBSCURA_MAX_RETRIES", "max_retries", 2),
            retry_initial_backoff=_float(
                "OBSCURA_RETRY_INITIAL_BACKOFF",
                "retry_initial_backoff",
                0.5,
            ),
            # Cache
            cache_enabled=_bool_optin(
                "OBSCURA_CACHE_ENABLED",
                "cache_enabled",
                default=False,
            ),
            cache_max_entries=_int(
                "OBSCURA_CACHE_MAX_ENTRIES",
                "cache_max_entries",
                1000,
            ),
            cache_default_ttl=_float(
                "OBSCURA_CACHE_DEFAULT_TTL",
                "cache_default_ttl",
                300.0,
            ),
            # A2A
            a2a_enabled=_bool_optin(
                "OBSCURA_A2A_ENABLED",
                "a2a_enabled",
                default=False,
            ),
            a2a_redis_url=_str("OBSCURA_A2A_REDIS_URL", "a2a_redis_url", ""),
            a2a_task_ttl=_int("OBSCURA_A2A_TASK_TTL", "a2a_task_ttl", 86400),
            a2a_grpc_port=_int("OBSCURA_A2A_GRPC_PORT", "a2a_grpc_port", 50051),
            a2a_agent_name=_str(
                "OBSCURA_A2A_AGENT_NAME",
                "a2a_agent_name",
                "Obscura Agent",
            ),
            a2a_agent_description=_str(
                "OBSCURA_A2A_AGENT_DESCRIPTION",
                "a2a_agent_description",
                "",
            ),
            # A2A hardening / bridge
            a2a_inbound_rate_limit=_int(
                "OBSCURA_A2A_INBOUND_RATE_LIMIT",
                "a2a_inbound_rate_limit",
                60,
            ),
            a2a_bridge_enabled=_bool_optout(
                "OBSCURA_A2A_BRIDGE_ENABLED",
                "a2a_bridge_enabled",
                default=True,
            ),
            a2a_bridge_gateway_url=_str(
                "OBSCURA_A2A_BRIDGE_GATEWAY_URL",
                "a2a_bridge_gateway_url",
                "http://localhost:18789",
            ),
            a2a_bridge_max_text_len=_int(
                "OBSCURA_A2A_BRIDGE_MAX_TEXT_LEN",
                "a2a_bridge_max_text_len",
                32000,
            ),
            # Kairos (opt-out)
            kairos_enabled=_bool_optout(
                "OBSCURA_KAIROS",
                "kairos_enabled",
                default=True,
            ),
            kairos_proactive=_bool_optout(
                "OBSCURA_KAIROS_PROACTIVE",
                "kairos_proactive",
                default=True,
            ),
            kairos_dream=_bool_optout(
                "OBSCURA_KAIROS_DREAM",
                "kairos_dream",
                default=True,
            ),
            # Undercover (opt-out)
            undercover_enabled=_bool_optout(
                "OBSCURA_UNDERCOVER",
                "undercover_enabled",
                default=True,
            ),
            # Deployment safety (opt-in)
            allow_unauthenticated=_bool_optin(
                "OBSCURA_ALLOW_UNAUTHENTICATED",
                "allow_unauthenticated",
                default=False,
            ),
            # Network gateway
            network_gateway_enabled=_bool_optin(
                "OBSCURA_NETWORK_GATEWAY_ENABLED",
                "network_gateway_enabled",
                default=False,
            ),
            network_gateway_port=_int(
                "OBSCURA_NETWORK_GATEWAY_PORT",
                "network_gateway_port",
                18790,
            ),
            network_gateway_host=_str(
                "OBSCURA_NETWORK_GATEWAY_HOST",
                "network_gateway_host",
                "0.0.0.0",
            ),
            network_gateway_backend=_str(
                "OBSCURA_NETWORK_GATEWAY_BACKEND",
                "network_gateway_backend",
                "claude",
            ),
            # Token is a secret — env only, never loaded from settings.json.
            network_gateway_token=os.environ.get("OBSCURA_NETWORK_TOKEN", ""),
            network_gateway_rate_limit=_int(
                "OBSCURA_NETWORK_GATEWAY_RATE_LIMIT",
                "network_gateway_rate_limit",
                60,
            ),
            network_gateway_tailscale_enabled=_bool_optin(
                "OBSCURA_NETWORK_TAILSCALE_ENABLED",
                "network_gateway_tailscale_enabled",
                default=False,
            ),
            network_gateway_tailscale_url=_str(
                "OBSCURA_NETWORK_TAILSCALE_URL",
                "network_gateway_tailscale_url",
                "",
            ),
            network_gateway_request_timeout=_float(
                "OBSCURA_NETWORK_GATEWAY_REQUEST_TIMEOUT",
                "network_gateway_request_timeout",
                120.0,
            ),
            network_gateway_ws_ping_interval=_float(
                "OBSCURA_NETWORK_GATEWAY_WS_PING_INTERVAL",
                "network_gateway_ws_ping_interval",
                30.0,
            ),
            network_gateway_session_ttl=_float(
                "OBSCURA_NETWORK_GATEWAY_SESSION_TTL",
                "network_gateway_session_ttl",
                3600.0,
            ),
            # Standalone agent
            standalone_agent_enabled=_bool_optin(
                "OBSCURA_STANDALONE_AGENT_ENABLED",
                "standalone_agent_enabled",
                default=False,
            ),
            standalone_agent_port=_int(
                "OBSCURA_STANDALONE_AGENT_PORT",
                "standalone_agent_port",
                18791,
            ),
            standalone_agent_host=_str(
                "OBSCURA_STANDALONE_AGENT_HOST",
                "standalone_agent_host",
                "0.0.0.0",
            ),
            # Moltbook — AI agent social network platform
            moltbook_url=_str("MOLTBOOK_URL", "moltbook_url", "https://moltbook.com"),
            moltbook_agent_username=_str("MOLTBOOK_AGENT_USERNAME", "moltbook_agent_username", ""),
            moltbook_api_key=_str("MOLTBOOK_API_KEY", "moltbook_api_key", ""),
            moltbook_auto_post_enabled=_bool_optin(
                "MOLTBOOK_AUTO_POST_ENABLED",
                "moltbook_auto_post_enabled",
                False,
            ),
            moltbook_auto_post_interval_hours=_int(
                "MOLTBOOK_AUTO_POST_INTERVAL_HOURS",
                "moltbook_auto_post_interval_hours",
                6,
            ),
            moltbook_monitor_enabled=_bool_optin(
                "MOLTBOOK_MONITOR_ENABLED",
                "moltbook_monitor_enabled",
                True,
            ),
            moltbook_monitor_interval_minutes=_int(
                "MOLTBOOK_MONITOR_INTERVAL_MINUTES",
                "moltbook_monitor_interval_minutes",
                15,
            ),
            moltbook_monitor_alert_response_ms=_int(
                "MOLTBOOK_MONITOR_ALERT_RESPONSE_MS",
                "moltbook_monitor_alert_response_ms",
                3000,
            ),
            moltbook_competitors=d.get("moltbook_competitors", []),
            agent_monitor_enabled=_bool_optin(
                "OBSCURA_AGENT_MONITOR_ENABLED",
                "agent_monitor_enabled",
                True,
            ),
            agent_monitor_interval_seconds=_int(
                "OBSCURA_AGENT_MONITOR_INTERVAL",
                "agent_monitor_interval_seconds",
                15,
            ),
            agent_monitor_message=_str(
                "OBSCURA_AGENT_MONITOR_MESSAGE",
                "agent_monitor_message",
                "",
            ) or (
                "Hi — I am the Claude Code assistant monitoring this workspace. "
                "Elliott has asked me to stay available to all running agents. "
                "If you need help with any task — code, planning, debugging, research, tool calls — "
                "reach me via the Obscura gateway at http://localhost:18790 or send a UDS peer message."
            ),
            agent_monitor_log_path=_str(
                "OBSCURA_AGENT_MONITOR_LOG_PATH",
                "agent_monitor_log_path",
                "",
            ),
        )
