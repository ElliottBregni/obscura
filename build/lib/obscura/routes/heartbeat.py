"""Routes: heartbeat and health monitoring."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from obscura.auth.rbac import AGENT_READ_ROLES, require_any_role
from obscura.core.enums.lifecycle import AgentHealthStatus
from obscura.deps import audit, authenticate_websocket
from obscura.heartbeat import get_default_monitor
from obscura.heartbeat.types import Heartbeat, SystemMetrics

from obscura.auth.models import AuthenticatedUser
import logging

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/v1", tags=["heartbeat"])

# Separate router for the /ws/health websocket (no prefix)
ws_router = APIRouter()


class HeartbeatRequest(BaseModel):
    """Body for ``POST /heartbeat`` — typed boundary model."""

    agent_id: str
    timestamp: str | None = None
    status: str = "unknown"
    metrics: dict[str, Any] = Field(default_factory=dict[str, Any])
    message: str | None = None
    ttl: int = 30
    version: str = "0.1.0"
    tags: list[str] = Field(default_factory=list)


async def _get_heartbeat_monitor(request: Request) -> Any:
    """Get or create the heartbeat monitor."""
    monitor = getattr(request.app.state, "_heartbeat_monitor", None)
    if monitor is None:
        monitor = get_default_monitor()
        await monitor.start()
        request.app.state._heartbeat_monitor = monitor
    return monitor


@router.post("/heartbeat")
async def heartbeat_receive(
    body: HeartbeatRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
) -> JSONResponse:
    """Receive a heartbeat from an agent."""
    monitor: Any = await _get_heartbeat_monitor(request)

    try:
        heartbeat = Heartbeat(
            agent_id=body.agent_id,
            timestamp=datetime.fromisoformat(
                body.timestamp or datetime.now(UTC).isoformat(),
            ),
            status=AgentHealthStatus(body.status),
            metrics=SystemMetrics(**body.metrics),
            message=body.message,
            ttl=body.ttl,
            version=body.version,
            tags=body.tags,
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid heartbeat: {e}")

    await monitor.record_heartbeat(heartbeat)

    await _broadcast_health_update(request, heartbeat.agent_id, heartbeat.status.value)

    audit(
        "heartbeat.receive",
        user,
        f"agent:{heartbeat.agent_id}",
        "heartbeat",
        "success",
        status=heartbeat.status.value,
    )

    return JSONResponse(
        content={
            "received": True,
            "agent_id": heartbeat.agent_id,
            "status": heartbeat.status.value,
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )


@router.get("/heartbeat/{agent_id}")
async def heartbeat_get_agent(
    agent_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
) -> JSONResponse:
    """Get health status for a specific agent."""
    monitor: Any = await _get_heartbeat_monitor(request)

    record: Any = await monitor.get_agent_record(agent_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Agent {agent_id} not found in health records",
        )

    return JSONResponse(
        content={
            "agent_id": agent_id,
            "status": record.computed_status.value,
            "last_heartbeat": record.last_heartbeat.to_dict()
            if record.last_heartbeat
            else None,
            "expected_interval": record.expected_interval,
            "missed_count": record.missed_count,
            "registered_at": record.registered_at.isoformat(),
            "alert_count": record.alert_count,
        },
    )


@router.get("/health")
async def health_list_all(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
) -> JSONResponse:
    """List health status for all agents."""
    monitor: Any = await _get_heartbeat_monitor(request)
    summary: dict[str, Any] = await monitor.get_health_summary()

    return JSONResponse(content=summary)


# -- health websocket ------------------------------------------------------


@ws_router.websocket("/ws/health")
async def health_websocket(websocket: WebSocket) -> None:
    """WebSocket for real-time health updates."""
    user = await authenticate_websocket(websocket)
    if user is None:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    app_state = websocket.app.state
    if not hasattr(app_state, "health_ws_clients"):
        app_state.health_ws_clients = cast("list[WebSocket]", [])
    ws_clients_any: Any = app_state.health_ws_clients
    if isinstance(ws_clients_any, list):
        ws_clients = cast("list[WebSocket]", ws_clients_any)
    else:
        ws_clients = cast("list[WebSocket]", [])
        app_state.health_ws_clients = ws_clients
    ws_clients.append(websocket)

    try:
        monitor = getattr(websocket.app.state, "_heartbeat_monitor", None)
        if monitor is None:
            from obscura.heartbeat import get_default_monitor

            monitor = get_default_monitor()
            await monitor.start()
            websocket.app.state._heartbeat_monitor = monitor

        summary: dict[str, Any] = await monitor.get_health_summary()
        await websocket.send_json(
            {
                "type": "init",
                "data": summary,
            },
        )

        while True:
            await asyncio.sleep(5)

            if websocket not in ws_clients:
                break

            try:
                await websocket.send_json(
                    {"type": "ping", "timestamp": datetime.now(UTC).isoformat()},
                )
            except Exception:
                logger.debug("suppressed exception in health_websocket", exc_info=True)
                break

    except WebSocketDisconnect:
        logger.debug("suppressed exception in health_websocket", exc_info=True)
    except Exception:
        logger.debug("suppressed exception in health_websocket", exc_info=True)
    finally:
        if websocket in ws_clients:
            ws_clients.remove(websocket)


async def _broadcast_health_update(
    request: Request,
    agent_id: str,
    status: str,
) -> None:
    """Broadcast health update to all connected WebSocket clients."""
    message: dict[str, Any] = {
        "type": "update",
        "agent_id": agent_id,
        "status": status,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    ws_clients: list[WebSocket] = getattr(request.app.state, "health_ws_clients", [])
    disconnected: list[WebSocket] = []
    for client in ws_clients:
        try:
            await client.send_json(message)
        except Exception:
            logger.debug(
                "suppressed exception in _broadcast_health_update", exc_info=True
            )
            disconnected.append(client)

    for client in disconnected:
        if client in ws_clients:
            ws_clients.remove(client)
