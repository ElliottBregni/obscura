"""
sdk.deps -- Shared FastAPI dependencies and helpers for route modules.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from fastapi import WebSocket
from sdk.auth.models import AuthenticatedUser
from sdk.client import ObscuraClient
from sdk.config import ObscuraConfig

if TYPE_CHECKING:
    from sdk.agent.agents import AgentRuntime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Client pool / factory
# ---------------------------------------------------------------------------


class ClientFactory:
    """Creates and manages per-request ObscuraClient instances."""

    def __init__(self, config: ObscuraConfig) -> None:
        self._config = config

    async def create(
        self,
        backend: str,
        *,
        user: AuthenticatedUser | None = None,
        model: str | None = None,
        model_alias: str | None = None,
        system_prompt: str = "",
    ) -> ObscuraClient:
        client = ObscuraClient(
            backend,
            model=model,
            model_alias=model_alias,
            system_prompt=system_prompt,
            user=user,
        )
        await client.start()
        return client


# ---------------------------------------------------------------------------
# Global agent runtime registry (keyed by user_id)
# ---------------------------------------------------------------------------

_runtimes: dict[str, "AgentRuntime"] = {}
_runtimes_lock = asyncio.Lock()


async def get_runtime(user: AuthenticatedUser) -> "AgentRuntime":
    """Get or create a persistent AgentRuntime for the given user."""
    from sdk.agent.agents import AgentRuntime  # load at call-time for test patching

    async with _runtimes_lock:
        if user.user_id not in _runtimes:
            runtime = AgentRuntime(user)
            await runtime.start()
            _runtimes[user.user_id] = runtime
        return _runtimes[user.user_id]


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------

# In-memory audit log storage (used by admin routes)
audit_logs: list[dict[str, Any]] = []
MAX_AUDIT_LOGS = 10000


def audit(
    event_type: str,
    user: AuthenticatedUser,
    resource: str,
    action: str,
    outcome: str,
    **details: Any,
) -> None:
    """Emit an audit event and store in memory."""
    from datetime import UTC, datetime

    # Emit to telemetry (best-effort)
    try:
        from sdk.telemetry.audit import AuditEvent, emit_audit_event

        emit_audit_event(
            AuditEvent(
                event_type=event_type,
                user_id=user.user_id,
                user_email=user.email,
                resource=resource,
                action=action,
                outcome=outcome,
                details=details,
            )
        )
    except Exception:
        pass

    # Store in memory for the /audit/logs endpoint
    log_entry: dict[str, str | dict[str, str] | None] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": event_type,
        "user_id": user.user_id,
        "user_email": user.email,
        "resource": resource,
        "action": action,
        "outcome": outcome,
        "details": details,
    }
    audit_logs.append(log_entry)
    if len(audit_logs) > MAX_AUDIT_LOGS:
        audit_logs.pop(0)

    # Trigger webhooks for important events (best-effort)
    if outcome in ["success", "failure"] and event_type in [
        "agent.spawn",
        "agent.stop",
        "agent.run",
        "workflow.execute",
        "memory.set",
        "memory.delete",
    ]:
        from sdk.routes.webhooks import trigger_webhooks

        asyncio.create_task(
            trigger_webhooks(
                event_type,
                {
                    "user_id": user.user_id,
                    "resource": resource,
                    "action": action,
                    "outcome": outcome,
                },
            )
        )


def record_sync_metric(status: str) -> None:
    """Record a sync_operations_total metric (best-effort)."""
    try:
        from sdk.telemetry.metrics import get_metrics

        m = get_metrics()
        m.sync_operations_total.add(1, {"status": status})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# WebSocket auth helper
# ---------------------------------------------------------------------------


async def authenticate_websocket(
    websocket: WebSocket,
) -> AuthenticatedUser | None:
    """Validate JWT token from WebSocket query params."""
    from sdk.auth.middleware import JWKSCache

    token = websocket.query_params.get("token", "")
    config: ObscuraConfig | None = getattr(websocket.app.state, "config", None)

    if config is None or not config.auth_enabled:
        from sdk.auth.rbac import AGENT_READ_ROLES

        return AuthenticatedUser(
            user_id="local-dev",
            email="dev@obscura.dev",
            roles=AGENT_READ_ROLES,
            org_id="local",
            token_type="user",
            raw_token=token,
        )

    try:
        import jwt as pyjwt

        jwks: JWKSCache = websocket.app.state.jwks_cache
        payload = pyjwt.decode(
            token,
            jwks.keys,  # type: ignore[arg-type]  # JWKS keys list accepted at runtime
            algorithms=["RS256"],
            audience=config.auth_audience,
            issuer=config.auth_issuer,
        )
        return AuthenticatedUser(
            user_id=payload.get("sub", "unknown"),
            email=payload.get("email", ""),
            roles=tuple(payload.get("urn:zitadel:iam:org:project:roles", {}).keys())
            or ("agent:read",),
            org_id=payload.get("org_id", ""),
            token_type="user",
            raw_token=token,
        )
    except Exception:
        logger.warning("WebSocket auth failed for token")
        return None
