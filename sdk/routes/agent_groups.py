"""Routes: agent groups, messaging, broadcast."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from sdk.auth.models import AuthenticatedUser
from sdk.auth.rbac import AGENT_READ_ROLES, AGENT_WRITE_ROLES, require_any_role
from sdk.deps import audit, get_runtime

router = APIRouter(prefix="/api/v1", tags=["agents"])

# In-memory group store
_agent_groups: dict[str, dict] = {}


@router.post("/agent-groups")
async def agent_group_create(
    body: dict,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Create an agent group."""
    group_id = str(uuid.uuid4())
    group = {
        "group_id": group_id,
        "name": body.get("name", "unnamed-group"),
        "agents": body.get("agents", []),
        "created_by": user.user_id,
        "created_at": datetime.now(UTC).isoformat(),
    }

    _agent_groups[group_id] = group

    audit("agent_group.create", user, f"group:{group_id}", "create", "success",
          name=group["name"])

    return JSONResponse(content=group)


@router.get("/agent-groups")
async def agent_group_list(
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """List all agent groups."""
    groups = list(_agent_groups.values())
    return JSONResponse(content={
        "groups": groups,
        "count": len(groups),
    })


@router.get("/agent-groups/{group_id}")
async def agent_group_get(
    group_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Get a specific agent group."""
    group = _agent_groups.get(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")
    return JSONResponse(content=group)


@router.delete("/agent-groups/{group_id}")
async def agent_group_delete(
    group_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Delete an agent group."""
    if group_id not in _agent_groups:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")

    del _agent_groups[group_id]

    audit("agent_group.delete", user, f"group:{group_id}", "delete", "success")

    return JSONResponse(content={"group_id": group_id, "deleted": True})


@router.post("/agent-groups/{group_id}/broadcast")
async def agent_group_broadcast(
    group_id: str,
    body: dict,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Broadcast a message to all agents in a group."""
    runtime = await get_runtime(user)

    group = _agent_groups.get(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")

    message = body.get("message", "")
    context = body.get("context", {})

    results = []
    errors = []

    for agent_id in group.get("agents", []):
        try:
            agent = runtime.get_agent(agent_id)
            if agent is None:
                errors.append({"agent_id": agent_id, "error": "Agent not found"})
                continue
            asyncio.create_task(agent.run(message, **context))
            results.append({"agent_id": agent_id, "status": "queued"})
        except Exception as e:
            errors.append({"agent_id": agent_id, "error": str(e)})

    return JSONResponse(content={
        "group_id": group_id,
        "message": message,
        "queued": results,
        "errors": errors,
    })


# -- agent messaging -------------------------------------------------------


@router.post("/agents/{from_agent}/send/{to_agent}")
async def agent_send_message(
    from_agent: str,
    to_agent: str,
    body: dict,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Send a message from one agent to another."""
    runtime = await get_runtime(user)

    source = runtime.get_agent(from_agent)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Source agent {from_agent} not found")

    target = runtime.get_agent(to_agent)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Target agent {to_agent} not found")

    message = body.get("message", "")

    await source.send_message(to_agent, message)

    audit("agent.message", user, f"agent:{from_agent}", "send", "success",
          to_agent=to_agent, message_preview=message[:100])

    return JSONResponse(content={
        "from_agent": from_agent,
        "to_agent": to_agent,
        "message": message,
        "sent": True,
    })


@router.get("/agents/{agent_id}/messages")
async def agent_get_messages(
    agent_id: str,
    limit: int = 100,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Get messages for an agent."""
    runtime = await get_runtime(user)

    agent = runtime.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    messages: list[dict] = []

    return JSONResponse(content={
        "agent_id": agent_id,
        "messages": messages,
        "count": len(messages),
    })
