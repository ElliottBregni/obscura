"""obscura.a2a.transports.jsonrpc — JSON-RPC 2.0 transport for A2A.

Mounts at ``/a2a/rpc`` and dispatches A2A methods:
    message/send, message/stream, tasks/get, tasks/list,
    tasks/cancel, tasks/subscribe, agent/authenticatedExtendedCard

Mirrors the pattern in ``sdk/mcp/server.py:1083-1149``.
"""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Request

from obscura.core.enums.protocol import A2AMethod, A2ARole, A2ATaskState
from obscura.core.models.protocol import (
    JSONRPCError,
    JSONRPCRequest,
    JSONRPCResponse,
)
from obscura.integrations.a2a.types import (
    A2AError,
    A2AMessage,
    TaskNotFoundError,
    TextPart,
)

if TYPE_CHECKING:
    from obscura.integrations.a2a.service import A2AService

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
        rpc_request = JSONRPCRequest.model_validate(body)
        method = rpc_request.method
        params: Mapping[str, Any] = rpc_request.params or {}

        try:
            result = await _dispatch(service, method, params)
            response = JSONRPCResponse(id=rpc_request.id, result=result)
            return response.model_dump(by_alias=True, exclude_none=True)
        except A2AError as e:
            logger.debug("suppressed exception in handle_rpc", exc_info=True)
            response = JSONRPCResponse(
                id=rpc_request.id,
                error=JSONRPCError(code=e.code, message=e.message, data=e.data),
            )
            return response.model_dump(by_alias=True, exclude_none=True)
        except Exception as e:
            logger.exception("A2A JSON-RPC error")
            response = JSONRPCResponse(
                id=rpc_request.id,
                error=JSONRPCError(
                    code=-32603,
                    message=f"Internal error: {e}",
                ),
            )
            return response.model_dump(by_alias=True, exclude_none=True)

    return router


async def _dispatch(
    service: A2AService,
    method: str,
    params: Mapping[str, Any],
) -> Any:
    """Dispatch a JSON-RPC method to the service layer."""
    if method == A2AMethod.MESSAGE_SEND.value:
        raw_message: Any = params.get("message")
        if raw_message is not None and not isinstance(raw_message, Mapping):
            raise A2AError(-32602, "params.message must be an object")
        message_payload: Mapping[str, Any] = (
            cast("Mapping[str, Any]", raw_message) if raw_message else {}
        )
        message = _parse_message(message_payload)
        raw_config: Any = params.get("configuration")
        blocking: bool = True
        push_url: str | None = None
        if isinstance(raw_config, Mapping):
            cfg = cast("Mapping[str, Any]", raw_config)
            blocking = bool(cfg.get("blocking", True))
            push_url = _optional_str(cfg, "pushNotificationUrl") or _optional_str(
                cfg, "x-push-url"
            )
        # Also accept pushNotificationUrl at the top-level params
        if push_url is None:
            push_url = _optional_str(params, "pushNotificationUrl")
        task = await service.message_send(
            message,
            context_id=_optional_str(params, "contextId"),
            task_id=_optional_str(params, "taskId"),
            blocking=blocking,
            push_notification_url=push_url,
        )
        return task.model_dump(mode="json")

    if method == A2AMethod.TASKS_GET.value:
        task_id = _optional_str(params, "taskId") or ""
        task = await service.tasks_get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task.model_dump(mode="json")

    if method == A2AMethod.TASKS_LIST.value:
        state_value: Any = params.get("state")
        limit_raw: Any = params.get("limit", 20) or 20
        tasks, cursor = await service.tasks_list(
            context_id=_optional_str(params, "contextId"),
            state=A2ATaskState(state_value) if isinstance(state_value, str) else None,
            cursor=_optional_str(params, "cursor"),
            limit=int(limit_raw),
        )
        result: dict[str, Any] = {
            "tasks": [t.model_dump(mode="json") for t in tasks],
        }
        if cursor:
            result["nextCursor"] = cursor
        return result

    if method == A2AMethod.TASKS_CANCEL.value:
        task_id = _optional_str(params, "taskId") or ""
        task = await service.tasks_cancel(task_id)
        return task.model_dump(mode="json")

    if method == A2AMethod.AGENT_CARD.value:
        card = service.get_agent_card()
        return card.model_dump(mode="json", by_alias=True, exclude_none=True)

    # message/stream is handled via SSE, not JSON-RPC response
    if method == A2AMethod.MESSAGE_STREAM.value:
        raise A2AError(
            -32600,
            "Use SSE endpoint for streaming: POST /a2a/v1/tasks/streaming",
        )

    if method == A2AMethod.TASKS_SUBSCRIBE.value:
        raise A2AError(
            -32600,
            "Use SSE endpoint for subscribe: POST /a2a/v1/tasks/{id}:subscribe",
        )

    raise A2AError(-32601, f"Method not found: {method}")


def _parse_message(data: Mapping[str, Any]) -> A2AMessage:
    """Parse a message from JSON-RPC params."""
    if not data:
        return A2AMessage(
            role=A2ARole.USER,
            messageId=str(uuid.uuid4()),
            parts=[TextPart(text="[empty]")],
        )
    return A2AMessage.model_validate(data)


def _optional_str(params: Mapping[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    return str(value)
