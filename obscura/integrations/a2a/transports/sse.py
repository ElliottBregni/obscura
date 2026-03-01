"""
obscura.a2a.transports.sse — Server-Sent Events transport for A2A.

Provides streaming endpoints:

    POST /a2a/v1/tasks/streaming          — message/stream (new task)
    POST /a2a/v1/tasks/{id}:subscribe     — subscribe to task updates

Returns SSE streams with A2A event types:
    status-update, artifact-update
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator

from fastapi import APIRouter
from pydantic import BaseModel

from obscura.integrations.a2a.service import A2AService
from obscura.integrations.a2a.types import (
    A2AError,
    A2AMessage,
    TaskArtifactUpdateEvent,
)

logger = logging.getLogger(__name__)


class StreamRequest(BaseModel):
    """Request body for SSE streaming endpoints."""

    message: dict[str, Any]
    contextId: str | None = None


def create_sse_router(service: A2AService) -> APIRouter:
    """Create a FastAPI router for A2A SSE streaming endpoints.

    Parameters
    ----------
    service:
        The A2AService instance that handles all business logic.
    """
    router = APIRouter(prefix="/a2a/v1", tags=["A2A SSE"])

    @router.post("/tasks/streaming")
    async def stream_task(body: StreamRequest) -> Any:
        """Stream a new task's execution as SSE events."""
        from sse_starlette.sse import EventSourceResponse

        message = A2AMessage.model_validate(body.message)

        async def event_generator() -> AsyncGenerator[dict[str, str], None]:
            try:
                async for event in service.message_stream(
                    message, context_id=body.contextId
                ):
                    event_type = "status-update"
                    if isinstance(event, TaskArtifactUpdateEvent):
                        event_type = "artifact-update"

                    yield {
                        "event": event_type,
                        "data": event.model_dump_json(),
                    }
            except A2AError as e:
                yield {
                    "event": "error",
                    "data": json.dumps({"code": e.code, "message": e.message}),
                }
            except Exception as e:
                logger.exception("SSE stream error")
                yield {
                    "event": "error",
                    "data": json.dumps({"code": -32603, "message": str(e)}),
                }

        return EventSourceResponse(event_generator())

    @router.post("/tasks/{task_id}:subscribe")
    async def subscribe_task(task_id: str) -> Any:
        """Subscribe to real-time updates for an existing task."""
        from sse_starlette.sse import EventSourceResponse

        async def event_generator() -> AsyncGenerator[dict[str, str], None]:
            try:
                async for event in service.tasks_subscribe(task_id):
                    event_type = "status-update"
                    if isinstance(event, TaskArtifactUpdateEvent):
                        event_type = "artifact-update"

                    yield {
                        "event": event_type,
                        "data": event.model_dump_json(),
                    }
            except A2AError as e:
                yield {
                    "event": "error",
                    "data": json.dumps({"code": e.code, "message": e.message}),
                }
            except Exception as e:
                logger.exception("SSE subscribe error")
                yield {
                    "event": "error",
                    "data": json.dumps({"code": -32603, "message": str(e)}),
                }

        return EventSourceResponse(event_generator())

    # Registered via decorator; reference to suppress reportUnusedFunction
    _ = stream_task, subscribe_task

    return router
