"""
sdk.a2a.transports.rest — REST transport for A2A.

Mounts at ``/a2a/v1`` with conventional REST endpoints:

    POST   /a2a/v1/tasks              — create task (message/send)
    GET    /a2a/v1/tasks/{id}         — get task
    GET    /a2a/v1/tasks              — list tasks
    POST   /a2a/v1/tasks/{id}:cancel  — cancel task
    GET    /a2a/v1/agent              — get agent card
    GET    /.well-known/agent.json    — well-known agent card (mounted separately)
"""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from sdk.a2a.service import A2AService
from sdk.a2a.types import (
    A2AError,
    A2AMessage,
    TaskState,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class SendMessageRequest(BaseModel):
    """REST request body for creating a task."""

    message: dict[str, Any]
    contextId: str | None = None
    taskId: str | None = None
    blocking: bool = True


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_rest_router(service: A2AService) -> APIRouter:
    """Create a FastAPI router for A2A REST endpoints.

    Parameters
    ----------
    service:
        The A2AService instance that handles all business logic.
    """
    router = APIRouter(prefix="/a2a/v1", tags=["A2A REST"])

    @router.post("/tasks")
    async def create_task(body: SendMessageRequest) -> dict[str, Any]:
        """Create a new task via message/send."""
        try:
            message = A2AMessage.model_validate(body.message)
            task = await service.message_send(
                message,
                context_id=body.contextId,
                task_id=body.taskId,
                blocking=body.blocking,
            )
            return task.model_dump(mode="json")
        except A2AError as e:
            raise HTTPException(status_code=_error_status(e.code), detail=e.message)

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        """Get a task by ID."""
        task = await service.tasks_get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
        return task.model_dump(mode="json")

    @router.get("/tasks")
    async def list_tasks(
        contextId: str | None = Query(default=None),
        state: str | None = Query(default=None),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        """List tasks with optional filtering."""
        task_state = TaskState(state) if state else None
        tasks, next_cursor = await service.tasks_list(
            context_id=contextId,
            state=task_state,
            cursor=cursor,
            limit=limit,
        )
        result: dict[str, Any] = {
            "tasks": [t.model_dump(mode="json") for t in tasks],
        }
        if next_cursor:
            result["nextCursor"] = next_cursor
        return result

    @router.post("/tasks/{task_id}:cancel")
    async def cancel_task(task_id: str) -> dict[str, Any]:
        """Cancel a task."""
        try:
            task = await service.tasks_cancel(task_id)
            return task.model_dump(mode="json")
        except A2AError as e:
            raise HTTPException(status_code=_error_status(e.code), detail=e.message)

    @router.get("/agent")
    async def get_agent_card() -> dict[str, Any]:
        """Get the agent card."""
        card = service.get_agent_card()
        return card.model_dump(mode="json")

    return router


def create_wellknown_router(service: A2AService) -> APIRouter:
    """Create a router for /.well-known/agent.json (mounted at app root)."""
    router = APIRouter(tags=["A2A Discovery"])

    @router.get("/.well-known/agent.json")
    async def wellknown_agent_card() -> dict[str, Any]:
        """A2A agent discovery endpoint."""
        return service.get_agent_card().model_dump(mode="json")

    return router


def _error_status(code: int) -> int:
    """Map A2A error codes to HTTP status codes."""
    if code == -32001:  # TaskNotFound
        return 404
    if code == -32002:  # TaskNotCancelable
        return 409
    if code == -32003:  # InvalidTransition
        return 409
    if code == -32005:  # VersionNotSupported
        return 400
    return 500
