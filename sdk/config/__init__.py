"""
sdk.config — Shared configuration for the Obscura platform.

Loads settings from environment variables, YAML files, or explicit values.
All three subsystems (auth, telemetry, infrastructure) contribute sections
to this unified config.
"""

from __future__ import annotations

import os
from typing import Self

from pydantic import BaseModel, model_validator


class ObscuraConfig(BaseModel):
    """Unified configuration for the Obscura platform.

    Each subsystem reads its own section. Resolved from environment
    variables when not set explicitly.
    """

    # Server
    host: str = "0.0.0.0"
    port: int = 8080

    # Auth (Zitadel)
    auth_enabled: bool = True
    auth_issuer: str = "http://zitadel:8080"
    auth_jwks_uri: str = ""  # defaults to {auth_issuer}/.well-known/jwks.json
    auth_audience: str = "obscura-sdk"

    # Telemetry (OpenTelemetry)
    otel_enabled: bool = True
    otel_endpoint: str = "http://otel-collector:4317"
    otel_service_name: str = "obscura-sdk"
    log_level: str = "INFO"
    log_format: str = "json"  # "json" | "text"

    # Backends
    default_backend: str = "copilot"

    # Capability system
    capability_secret: str = ""  # HMAC signing key; empty = random per-process
    capability_ttl: int = 3600  # Token lifetime in seconds

    # A2A (Agent-to-Agent protocol)
    a2a_enabled: bool = False
    a2a_redis_url: str = ""  # empty = use InMemoryTaskStore
    a2a_task_ttl: int = 86400  # TTL for completed tasks in Redis (seconds)
    a2a_grpc_port: int = 50051  # gRPC server port (0 = disabled)
    a2a_agent_name: str = "Obscura Agent"
    a2a_agent_description: str = ""

    @model_validator(mode="after")
    def _set_jwks_uri(self) -> Self:
        if not self.auth_jwks_uri:
            self.auth_jwks_uri = f"{self.auth_issuer.rstrip('/')}/.well-known/jwks.json"
        return self

    @classmethod
    def from_env(cls) -> ObscuraConfig:
        """Build config from environment variables with sensible defaults."""
        return cls(
            host=os.environ.get("OBSCURA_HOST", "0.0.0.0"),
            port=int(os.environ.get("OBSCURA_PORT", "8080")),
            # Auth
            auth_enabled=os.environ.get("OBSCURA_AUTH_ENABLED", "true").lower()
            == "true",
            auth_issuer=os.environ.get("OBSCURA_AUTH_ISSUER", "http://zitadel:8080"),
            auth_jwks_uri=os.environ.get("OBSCURA_AUTH_JWKS_URI", ""),
            auth_audience=os.environ.get("OBSCURA_AUTH_AUDIENCE", "obscura-sdk"),
            # Telemetry
            otel_enabled=os.environ.get("OTEL_ENABLED", "true").lower() == "true",
            otel_endpoint=os.environ.get(
                "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317"
            ),
            otel_service_name=os.environ.get("OTEL_SERVICE_NAME", "obscura-sdk"),
            log_level=os.environ.get("OBSCURA_LOG_LEVEL", "INFO"),
            log_format=os.environ.get("OBSCURA_LOG_FORMAT", "json"),
            # Backends
            default_backend=os.environ.get("OBSCURA_DEFAULT_BACKEND", "copilot"),
            # Capability system
            capability_secret=os.environ.get("OBSCURA_CAPABILITY_SECRET", ""),
            capability_ttl=int(os.environ.get("OBSCURA_CAPABILITY_TTL", "3600")),
            # A2A
            a2a_enabled=os.environ.get("OBSCURA_A2A_ENABLED", "false").lower() == "true",
            a2a_redis_url=os.environ.get("OBSCURA_A2A_REDIS_URL", ""),
            a2a_task_ttl=int(os.environ.get("OBSCURA_A2A_TASK_TTL", "86400")),
            a2a_grpc_port=int(os.environ.get("OBSCURA_A2A_GRPC_PORT", "50051")),
            a2a_agent_name=os.environ.get("OBSCURA_A2A_AGENT_NAME", "Obscura Agent"),
            a2a_agent_description=os.environ.get("OBSCURA_A2A_AGENT_DESCRIPTION", ""),
        )
