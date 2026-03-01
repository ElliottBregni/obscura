"""Routes: heartbeat and health monitoring."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse

from obscura.auth.models import AuthenticatedUser
from obscura.auth.rbac import AGENT_READ_ROLES, require_any_role
from obscura.deps import audit, authenticate_websocket

router = APIRouter(prefix="/api/v1", tags=["heartbeat"])

# Separate router for the /ws/health websocket (no prefix)
ws_router = APIRouter()


async def _get_heartbeat_monitor(request: Request) -> Any:
    """Get or create the heartbeat monitor."""
    monitor = getattr(request.app.state, "heartbeat_monitor", None)
    if monitor is None:
        from obscura.heartbeat import get_default_monitor

        monitor = get_default_monitor()
        await monitor.start()
        setattr(request.app.state, "heartbeat_monitor", monitor)
    return monitor


@router.post("/heartbeat")
async def heartbeat_receive(
    body: dict[str, Any],
    request: Request,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Receive a heartbeat from an agent."""
    from obscura.heartbeat.types import Heartbeat, HealthStatus, SystemMetrics

    monitor: Any = await _get_heartbeat_monitor(request)

    try:
        heartbeat = Heartbeat(
            agent_id=body["agent_id"],
            timestamp=datetime.fromisoformat(
                body.get("timestamp", datetime.now(UTC).isoformat())
            ),
            status=HealthStatus(body.get("status", "unknown")),
            metrics=SystemMetrics(**body.get("metrics", {})),
            message=body.get("message"),
            ttl=body.get("ttl", 30),
            version=body.get("version", "0.1.0"),
            tags=body.get("tags", []),
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
        }
    )


@router.get("/heartbeat/{agent_id}")
async def heartbeat_get_agent(
    agent_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Get health status for a specific agent."""
    monitor: Any = await _get_heartbeat_monitor(request)

    record: Any = await monitor.get_agent_record(agent_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"Agent {agent_id} not found in health records"
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
        }
    )


@router.get("/health")
async def health_list_all(
    request: Request,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
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
    if not hasattr(websocket.app.state, "health_ws_clients"):
        websocket.app.state.health_ws_clients = []
    ws_clients_any = getattr(websocket.app.state, "health_ws_clients")
    if isinstance(ws_clients_any, list):
        ws_clients = cast(list[WebSocket], ws_clients_any)
    else:
        ws_clients: list[WebSocket] = []
        websocket.app.state.health_ws_clients = ws_clients
    ws_clients.append(websocket)

    try:
        monitor = getattr(websocket.app.state, "heartbeat_monitor", None)
        if monitor is None:
            from obscura.heartbeat import get_default_monitor

            monitor = get_default_monitor()
            await monitor.start()
            setattr(websocket.app.state, "heartbeat_monitor", monitor)

        summary: dict[str, Any] = await monitor.get_health_summary()
        await websocket.send_json(
            {
                "type": "init",
                "data": summary,
            }
        )

        while True:
            await asyncio.sleep(5)

            if websocket not in ws_clients:
                break

            try:
                await websocket.send_json(
                    {"type": "ping", "timestamp": datetime.now(UTC).isoformat()}
                )
            except Exception:
                break

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if websocket in ws_clients:
            ws_clients.remove(websocket)


async def _broadcast_health_update(
    request: Request, agent_id: str, status: str
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
            disconnected.append(client)

    for client in disconnected:
        if client in ws_clients:
            ws_clients.remove(client)
