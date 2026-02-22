"""
obscura.a2a.transports.jsonrpc — JSON-RPC 2.0 transport for A2A.

Mounts at ``/a2a/rpc`` and dispatches A2A methods:
    message/send, message/stream, tasks/get, tasks/list,
    tasks/cancel, tasks/subscribe, agent/authenticatedExtendedCard

Mirrors the pattern in ``sdk/mcp/server.py:1083-1149``.
"""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from obscura.integrations.a2a.service import A2AService
from obscura.integrations.a2a.types import (
    A2AError,
    A2AMessage,
    A2AMethod,
    TaskState,
    TextPart,
)

logger = logging.getLogger(__name__)

# A2A protocol version
A2A_PROTOCOL_VERSION = "0.3"


def create_jsonrpc_router(service: A2AService) -> APIRouter:
    """Create a FastAPI router for A2A JSON-RPC 2.0 endpoint.

    Parameters
    ----------
    service:
        The A2AService instance that handles all business logic.
    """
    router = APIRouter(prefix="/a2a", tags=["A2A JSON-RPC"])

    @router.post("/rpc")
    async def handle_rpc(request: Request) -> dict[str, Any]:
        """Handle A2A JSON-RPC 2.0 requests."""
        body = await request.json()

        method = body.get("method", "")
        params = body.get("params", {})
        req_id = body.get("id")

        try:
            result = await _dispatch(service, method, params)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": result,
            }
        except A2AError as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": e.code,
                    "message": e.message,
                    "data": e.data,
                },
            }
        except Exception as e:
            logger.exception("A2A JSON-RPC error")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": f"Internal error: {e}",
                },
            }

    return router


async def _dispatch(
    service: A2AService, method: str, params: dict[str, Any],
) -> Any:
    """Dispatch a JSON-RPC method to the service layer."""
    if method == A2AMethod.MESSAGE_SEND.value:
        message = _parse_message(params.get("message", {}))
        task = await service.message_send(
            message,
            context_id=params.get("contextId"),
            task_id=params.get("taskId"),
            blocking=params.get("configuration", {}).get("blocking", True),
        )
        return task.model_dump(mode="json")

    if method == A2AMethod.TASKS_GET.value:
        task_id = params.get("taskId", "")
        task = await service.tasks_get(task_id)
        if task is None:
            from obscura.integrations.a2a.types import TaskNotFoundError
            raise TaskNotFoundError(task_id)
        return task.model_dump(mode="json")

    if method == A2AMethod.TASKS_LIST.value:
        tasks, cursor = await service.tasks_list(
            context_id=params.get("contextId"),
            state=TaskState(params["state"]) if "state" in params else None,
            cursor=params.get("cursor"),
            limit=params.get("limit", 20),
        )
        result: dict[str, Any] = {
            "tasks": [t.model_dump(mode="json") for t in tasks],
        }
        if cursor:
            result["nextCursor"] = cursor
        return result

    if method == A2AMethod.TASKS_CANCEL.value:
        task_id = params.get("taskId", "")
        task = await service.tasks_cancel(task_id)
        return task.model_dump(mode="json")

    if method == A2AMethod.AGENT_CARD.value:
        card = service.get_agent_card()
        return card.model_dump(mode="json")

    # message/stream is handled via SSE, not JSON-RPC response
    if method == A2AMethod.MESSAGE_STREAM.value:
        raise A2AError(-32600, "Use SSE endpoint for streaming: POST /a2a/v1/tasks/streaming")

    if method == A2AMethod.TASKS_SUBSCRIBE.value:
        raise A2AError(-32600, "Use SSE endpoint for subscribe: POST /a2a/v1/tasks/{id}:subscribe")

    raise A2AError(-32601, f"Method not found: {method}")


def _parse_message(data: dict[str, Any]) -> A2AMessage:
    """Parse a message from JSON-RPC params."""
    if not data:
        return A2AMessage(
            role="user",
            messageId="auto",
            parts=[TextPart(text="[empty]")],
        )
    return A2AMessage.model_validate(data)
