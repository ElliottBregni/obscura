"""obscura.deps -- Shared FastAPI dependencies and helpers for route modules."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import Request

from obscura.auth.models import AuthenticatedUser

if TYPE_CHECKING:
    from fastapi import WebSocket

    from obscura.agent.agents import AgentRuntime
    from obscura.composition.session import AgentSession
    from obscura.core.config import ObscuraConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session factory (per-request agent composition)
# ---------------------------------------------------------------------------


class ClientFactory:
    """Builds per-request `AgentSession`s for API route handlers.

    Each call returns a fully composed session (plugin tools registered,
    capability resolver attached, etc.). Routes use it via ``async with``
    so resources are torn down after the request.

    Historical note: this used to return a bare ``ObscuraClient``. The
    composition refactor moved tool/plugin/hook wiring out of
    ``ObscuraClient.__init__`` into ``build_api_session``, so the factory
    now returns the higher-level ``AgentSession``.
    """

    def __init__(self, config: ObscuraConfig) -> None:
        self._config = config

    async def create_session(
        self,
        backend: str,
        *,
        user: AuthenticatedUser | None = None,
        model: str | None = None,
        model_alias: str | None = None,
        system_prompt: str = "",
        oauth_github_token: str | None = None,
    ) -> AgentSession:
        """Build an `AgentSession` for one request.

        ``oauth_github_token`` is the Supabase-forwarded GitHub token from
        an ``X-GitHub-Token`` header. When set it becomes the "easy path"
        fallback for Copilot auth — env vars still override.

        If Copilot has recently 403'd this user's OAuth token, the token
        is dropped before the resolver runs so we don't waste a round-trip
        on a known-bad credential. On a Copilot auth failure we retry once
        without the OAuth fallback so the user's call still succeeds via
        env/CLI-sourced tokens.
        """
        from obscura.auth.copilot_403_cache import (
            is_oauth_token_blocked,
            mark_oauth_token_blocked,
        )
        from obscura.composition.api import build_api_session
        from obscura.composition.session import SessionConfig
        from obscura.core.auth import AuthConfig

        if user is None:
            msg = "API session requires authenticated user"
            raise RuntimeError(msg)

        effective_oauth_token = oauth_github_token
        if effective_oauth_token and is_oauth_token_blocked(
            user.user_id,
            effective_oauth_token,
        ):
            logger.debug(
                "Dropping OAuth GitHub token for user %s — Copilot 403 cached",
                user.user_id,
            )
            effective_oauth_token = None

        auth = (
            AuthConfig(oauth_github_token=effective_oauth_token)
            if effective_oauth_token
            else None
        )

        config = SessionConfig(
            backend=backend,
            model=model,
            system_prompt=system_prompt,
            extras={"model_alias": model_alias} if model_alias else {},
        )

        try:
            return await build_api_session(config, user=user, auth=auth)
        except Exception as exc:
            if effective_oauth_token and _looks_like_copilot_auth_failure(exc):
                logger.info(
                    "Copilot rejected Supabase OAuth token for user %s; "
                    "falling back to env/CLI resolver",
                    user.user_id,
                )
                mark_oauth_token_blocked(user.user_id, effective_oauth_token)
                return await build_api_session(config, user=user, auth=None)
            raise


_COPILOT_AUTH_FAILURE_MARKERS = (
    "403",
    "forbidden",
    "unauthorized",
    "copilot",
    "bad credentials",
)


def _looks_like_copilot_auth_failure(exc: Exception) -> bool:
    """Heuristic: did this exception come from Copilot rejecting the token?

    The ``github-copilot-sdk`` raises various error types for auth failures,
    not a single exception class. We match on message content rather than
    type to stay robust against SDK changes. False positives here just mean
    we pessimistically mark a token bad for 5 minutes — not catastrophic.
    """
    msg = str(exc).lower()
    return any(marker in msg for marker in _COPILOT_AUTH_FAILURE_MARKERS)


# ---------------------------------------------------------------------------
# FastAPI dependency: extract Supabase-forwarded GitHub OAuth token
# ---------------------------------------------------------------------------


def get_oauth_github_token(request: Request) -> str | None:
    """FastAPI dep that reads ``X-GitHub-Token`` from the request.

    Returns ``None`` when the header is absent so downstream code can fall
    back to env/CLI sources without special-casing.
    """
    token = request.headers.get("X-GitHub-Token") or request.headers.get(
        "x-github-token",
    )
    if not token:
        return None
    token = token.strip()
    return token or None


# ---------------------------------------------------------------------------
# Global agent runtime registry (keyed by user_id)
# ---------------------------------------------------------------------------

_runtimes: dict[str, AgentRuntime] = {}
_runtimes_lock = asyncio.Lock()


async def get_runtime(user: AuthenticatedUser) -> AgentRuntime:
    """Get or create a persistent AgentRuntime for the given user."""
    from obscura.agent.agents import AgentRuntime  # load at call-time for test patching

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


def reset_audit_logs() -> None:
    """Clear audit logs. Used by test fixtures to prevent cross-test pollution."""
    audit_logs.clear()


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
        from obscura.telemetry.audit import AuditEvent, emit_audit_event

        emit_audit_event(
            AuditEvent(
                event_type=event_type,
                user_id=user.user_id,
                user_email=user.email,
                resource=resource,
                action=action,
                outcome=outcome,
                details=details,
            ),
        )
    except Exception:
        logger.debug("suppressed exception in audit", exc_info=True)

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
        from obscura.routes.webhooks import trigger_webhooks

        asyncio.create_task(
            trigger_webhooks(
                event_type,
                {
                    "user_id": user.user_id,
                    "resource": resource,
                    "action": action,
                    "outcome": outcome,
                },
            ),
        )


def record_sync_metric(status: str) -> None:
    """Record a sync_operations_total metric (best-effort)."""
    try:
        from obscura.telemetry.metrics import get_metrics

        m = get_metrics()
        m.sync_operations_total.add(1, {"status": status})
    except Exception:
        logger.debug("suppressed exception in record_sync_metric", exc_info=True)


# ---------------------------------------------------------------------------
# WebSocket auth helper
# ---------------------------------------------------------------------------


async def authenticate_websocket(
    websocket: WebSocket,
) -> AuthenticatedUser | None:
    """Validate API key from WebSocket query params.

    Browsers can't set arbitrary headers in the WebSocket handshake, so we
    accept the API key via a query param. Returns None if authentication fails.
    """
    from obscura.auth.rbac import user_from_api_key

    api_key = websocket.query_params.get("api_key", "")
    api_user = user_from_api_key(api_key)
    if api_user is not None:
        return api_user

    logger.warning("WebSocket auth failed: no valid API key")
    return None
