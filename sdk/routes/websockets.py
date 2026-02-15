"""Routes: WebSocket endpoints (agent, monitor, broadcast, memory watch)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from sdk.deps import authenticate_websocket, get_runtime

router = APIRouter()

# Broadcast state
_broadcast_clients: list[WebSocket] = []

# Memory watch state
_memory_watch_clients: dict[str, list[WebSocket]] = {}


# Testing/observability helpers (read-only accessors)
def broadcast_clients() -> list[WebSocket]:
    """Mutable list of broadcast websocket clients (for tests/metrics)."""
    return _broadcast_clients


def clear_broadcast_clients() -> None:
    """Clear broadcast clients (testing helper)."""
    _broadcast_clients.clear()


def memory_watch_clients() -> dict[str, list[WebSocket]]:
    """Mutable mapping of namespace -> clients (for tests/metrics)."""
    return _memory_watch_clients


def clear_memory_watch_clients() -> None:
    """Clear memory watch clients (testing helper)."""
    _memory_watch_clients.clear()


# -- agent websocket -------------------------------------------------------


@router.websocket("/ws/agents/{agent_id}")
async def agent_websocket(
    websocket: WebSocket,
    agent_id: str,
) -> None:
    """WebSocket for real-time agent communication."""
    user = await authenticate_websocket(websocket)
    if user is None:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()

    try:
        runtime = await get_runtime(user)
        agent = runtime.get_agent(agent_id)

        if agent is None:
            await websocket.send_json(
                {"type": "error", "message": f"Agent {agent_id} not found"}
            )
            await websocket.close()
            return

        while True:
            message: dict[str, Any] = await websocket.receive_json()

            if message.get("type") == "run":
                prompt: str = message.get("prompt", "")
                context: dict[str, Any] = message.get("context", {})
                try:
                    async for chunk in agent.stream(prompt, **context):
                        await websocket.send_json(
                            {
                                "type": "chunk",
                                "text": chunk,
                            }
                        )
                    await websocket.send_json({"type": "done"})
                except Exception as e:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": str(e),
                        }
                    )

            elif message.get("type") == "status":
                state = agent.get_state()
                await websocket.send_json(
                    {
                        "type": "status",
                        "status": state.status.name,
                        "iteration_count": state.iteration_count,
                    }
                )

            elif message.get("type") == "stop":
                await agent.stop()
                await websocket.send_json(
                    {
                        "type": "status",
                        "status": "STOPPED",
                    }
                )
                break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": str(e),
                }
            )
        except Exception:
            pass


# -- monitor websocket -----------------------------------------------------


@router.websocket("/ws/monitor")
async def monitor_websocket(websocket: WebSocket) -> None:
    """WebSocket for monitoring all agents."""
    user = await authenticate_websocket(websocket)
    if user is None:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()

    try:
        runtime = await get_runtime(user)

        agents = runtime.list_agents()
        await websocket.send_json(
            {
                "type": "init",
                "agents": [
                    {
                        "agent_id": a.id,
                        "name": a.config.name,
                        "status": a.status.name,
                        "model": a.config.model,
                    }
                    for a in agents
                ],
            }
        )

        while True:
            await asyncio.sleep(5)
            agents = runtime.list_agents()
            await websocket.send_json(
                {
                    "type": "update",
                    "agents": [
                        {
                            "agent_id": a.id,
                            "name": a.config.name,
                            "status": a.status.name,
                            "model": a.config.model,
                        }
                        for a in agents
                    ],
                }
            )

    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# -- broadcast websocket ---------------------------------------------------


@router.websocket("/ws/broadcast")
async def broadcast_websocket(websocket: WebSocket) -> None:
    """WebSocket for system-wide broadcast events."""
    user = await authenticate_websocket(websocket)
    if user is None:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    _broadcast_clients.append(websocket)

    try:
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_text("pong")

    except WebSocketDisconnect:
        _broadcast_clients.remove(websocket)
    except Exception:
        if websocket in _broadcast_clients:
            _broadcast_clients.remove(websocket)


async def broadcast_event(event_type: str, data: dict[str, Any]) -> None:
    """Broadcast an event to all connected clients."""
    message: dict[str, Any] = {
        "type": event_type,
        "timestamp": datetime.now(UTC).isoformat(),
        "data": data,
    }

    disconnected: list[WebSocket] = []
    for client in _broadcast_clients:
        try:
            await client.send_json(message)
        except Exception:
            disconnected.append(client)

    for client in disconnected:
        if client in _broadcast_clients:
            _broadcast_clients.remove(client)


# -- memory watch websocket ------------------------------------------------


@router.websocket("/ws/memory/{namespace}")
async def memory_watch_websocket(
    websocket: WebSocket,
    namespace: str,
) -> None:
    """WebSocket for watching memory changes in a namespace."""
    user = await authenticate_websocket(websocket)
    if user is None:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()

    if namespace not in _memory_watch_clients:
        _memory_watch_clients[namespace] = []
    _memory_watch_clients[namespace].append(websocket)

    try:
        from sdk.memory import MemoryStore

        store = MemoryStore.for_user(user)
        keys = store.list_keys(namespace=namespace)

        await websocket.send_json(
            {
                "type": "init",
                "namespace": namespace,
                "keys": [{"namespace": k.namespace, "key": k.key} for k in keys],
            }
        )

        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_text("pong")

    except WebSocketDisconnect:
        if namespace in _memory_watch_clients:
            if websocket in _memory_watch_clients[namespace]:
                _memory_watch_clients[namespace].remove(websocket)
    except Exception:
        if namespace in _memory_watch_clients:
            if websocket in _memory_watch_clients[namespace]:
                _memory_watch_clients[namespace].remove(websocket)


async def notify_memory_change(namespace: str, event_type: str, key: str) -> None:
    """Notify all watchers of a memory change."""
    if namespace not in _memory_watch_clients:
        return

    message: dict[str, Any] = {
        "type": event_type,
        "namespace": namespace,
        "key": key,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    disconnected: list[WebSocket] = []
    for client in _memory_watch_clients[namespace]:
        try:
            await client.send_json(message)
        except Exception:
            disconnected.append(client)

    for client in disconnected:
        if client in _memory_watch_clients[namespace]:
            _memory_watch_clients[namespace].remove(client)
