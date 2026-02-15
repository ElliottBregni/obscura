"""Routes: audit logs, metrics, rate limits."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from sdk.auth.models import AuthenticatedUser
from sdk.auth.rbac import require_any_role, require_role
from sdk.deps import audit_logs, get_runtime

router = APIRouter(prefix="/api/v1", tags=["admin"])

# In-memory rate limit store
_rate_limits: dict[str, dict[str, Any]] = {}


# -- audit logs ------------------------------------------------------------


@router.get("/audit/logs")
async def audit_logs_list(
    start: str | None = None,
    end: str | None = None,
    user_id: str | None = None,
    resource: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
    limit: int = 100,
    offset: int = 0,
    user: AuthenticatedUser = Depends(require_role("admin")),
) -> JSONResponse:
    """Query audit logs. Admin only."""
    logs: list[dict[str, Any]] = list(audit_logs)

    if start:
        logs = [l for l in logs if l.get("timestamp", "") >= start]
    if end:
        logs = [l for l in logs if l.get("timestamp", "") <= end]
    if user_id:
        logs = [l for l in logs if l.get("user_id") == user_id]
    if resource:
        logs = [l for l in logs if resource in l.get("resource", "")]
    if action:
        logs = [l for l in logs if l.get("action") == action]
    if outcome:
        logs = [l for l in logs if l.get("outcome") == outcome]

    logs = sorted(logs, key=lambda x: x.get("timestamp", ""), reverse=True)

    total = len(logs)
    logs = logs[offset:offset + limit]

    return JSONResponse(content={
        "logs": logs,
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@router.get("/audit/logs/summary")
async def audit_logs_summary(
    user: AuthenticatedUser = Depends(require_role("admin")),
) -> JSONResponse:
    """Get audit log summary. Admin only."""
    actions: Counter[str | None] = Counter(l.get("action") for l in audit_logs)
    outcomes: Counter[str | None] = Counter(l.get("outcome") for l in audit_logs)

    cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    recent: list[dict[str, Any]] = [l for l in audit_logs if l.get("timestamp", "") > cutoff]

    return JSONResponse(content={
        "total_logs": len(audit_logs),
        "actions": dict(actions),
        "outcomes": dict(outcomes),
        "last_24h": len(recent),
    })


# -- metrics ---------------------------------------------------------------


@router.get("/metrics")
async def metrics_get(
    user: AuthenticatedUser = Depends(require_any_role("admin", "agent:read")),
) -> JSONResponse:
    """Get system metrics."""
    runtime = await get_runtime(user)

    agents = runtime.list_agents()
    agent_stats: dict[str, Any] = {
        "total": len(agents),
        "by_status": {},
        "by_model": {},
    }

    by_status: dict[str, int] = agent_stats["by_status"]
    by_model: dict[str, int] = agent_stats["by_model"]
    for agent in agents:
        status = agent.status.name
        model = agent.config.model
        by_status[status] = by_status.get(status, 0) + 1
        by_model[model] = by_model.get(model, 0) + 1

    from sdk.memory import MemoryStore
    store = MemoryStore.for_user(user)
    memory_stats = store.get_stats()

    from sdk.routes.agents import _agent_templates
    from sdk.routes.workflows import _workflows, _workflow_executions
    from sdk.routes.webhooks import _webhooks

    return JSONResponse(content={
        "agents": agent_stats,
        "memory": memory_stats,
        "templates": {
            "total_templates": len(_agent_templates),
        },
        "workflows": {
            "total_workflows": len(_workflows),
            "total_executions": len(_workflow_executions),
        },
        "webhooks": {
            "total": len(_webhooks),
            "active": sum(1 for w in _webhooks.values() if w.get("active", True)),
        },
        "timestamp": datetime.now(UTC).isoformat(),
    })


@router.get("/metrics/agents/{agent_id}")
async def metrics_agent_get(
    agent_id: str,
    user: AuthenticatedUser = Depends(require_any_role("admin", "agent:read")),
) -> JSONResponse:
    """Get metrics for a specific agent."""
    runtime = await get_runtime(user)

    agent = runtime.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    state = agent.get_state()

    return JSONResponse(content={
        "agent_id": agent_id,
        "name": state.name,
        "status": state.status.name,
        "created_at": state.created_at.isoformat(),
        "updated_at": state.updated_at.isoformat(),
        "iteration_count": state.iteration_count,
        "error_message": state.error_message,
    })


# -- rate limits -----------------------------------------------------------


@router.get("/rate-limits")
async def rate_limits_get(
    user: AuthenticatedUser = Depends(require_role("admin")),
) -> JSONResponse:
    """Get current rate limits. Admin only."""
    return JSONResponse(content={
        "default": {
            "requests_per_minute": 100,
            "concurrent_agents": 10,
            "memory_quota_mb": 1024,
        },
        "custom": _rate_limits,
    })


@router.post("/rate-limits")
async def rate_limits_set(
    body: dict[str, Any],
    user: AuthenticatedUser = Depends(require_role("admin")),
) -> JSONResponse:
    """Set rate limits for an API key. Admin only."""
    api_key: str | None = body.get("api_key")
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")

    _rate_limits[api_key] = {
        "requests_per_minute": body.get("requests_per_minute", 100),
        "concurrent_agents": body.get("concurrent_agents", 10),
        "memory_quota_mb": body.get("memory_quota_mb", 1024),
        "set_by": user.user_id,
        "set_at": datetime.now(UTC).isoformat(),
    }

    return JSONResponse(content={
        "api_key": api_key[:8] + "...",
        "limits": _rate_limits[api_key],
    })


@router.delete("/rate-limits/{api_key}")
async def rate_limits_delete(
    api_key: str,
    user: AuthenticatedUser = Depends(require_role("admin")),
) -> JSONResponse:
    """Delete custom rate limits for an API key. Admin only."""
    if api_key in _rate_limits:
        del _rate_limits[api_key]

    return JSONResponse(content={"api_key": api_key[:8] + "...", "deleted": True})
