"""Routes: Kairos autonomous goal runtime."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from obscura.core.kairos import GoalBudget, GoalStatus
from obscura.core.paths import resolve_obscura_home

router = APIRouter(prefix="/api/v1", tags=["goals"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_store():
    """Return a fresh GoalStore bound to the default kairos.db path."""
    from obscura.core.kairos import GoalStore  # lazy import avoids circular

    db_path = resolve_obscura_home() / "kairos.db"
    return GoalStore(str(db_path))


def _goal_to_dict(goal: Any) -> dict[str, Any]:
    """Serialize a Goal dataclass to a JSON-safe dict."""
    return {
        "id": goal.goal_id,
        "title": goal.title,
        "description": goal.description,
        "status": goal.status.value if hasattr(goal.status, "value") else goal.status,
        "priority": goal.priority,
        "success_criteria": list(goal.success_criteria) if goal.success_criteria else [],
        "tags": list(goal.tags) if goal.tags else [],
        "budget": {
            "max_tasks": goal.budget.max_tasks,
            "max_turns": goal.budget.max_turns,
            "max_wall_seconds": goal.budget.max_wall_seconds,
            "max_tokens": goal.budget.max_tokens,
        } if goal.budget else None,
        "created_at": goal.created_at.isoformat() if isinstance(goal.created_at, datetime) else goal.created_at,
        "updated_at": goal.updated_at.isoformat() if isinstance(goal.updated_at, datetime) else goal.updated_at,
        "started_at": goal.started_at.isoformat() if isinstance(goal.started_at, datetime) else goal.started_at if goal.started_at else None,
        "completed_at": goal.completed_at.isoformat() if isinstance(goal.completed_at, datetime) else goal.completed_at if goal.completed_at else None,
        "error": goal.error,
        "metadata": dict(goal.metadata) if goal.metadata else {},
    }


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class BudgetRequest(BaseModel):
    max_tasks: int = Field(0, ge=0, description="0 = unlimited")
    max_turns: int = Field(0, ge=0)
    max_wall_seconds: float = Field(0.0, ge=0.0)
    max_tokens: int = Field(0, ge=0)


class CreateGoalRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    description: str = Field("", max_length=4096)
    priority: int = Field(50, ge=0, le=100)
    success_criteria: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    budget: BudgetRequest | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/goals")
async def list_goals(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List goals, optionally filtered by status."""
    store = _get_store()
    try:
        status_filter = GoalStatus(status) if status else None
        goals = store.list_goals(status=status_filter, limit=limit, offset=offset)
        return {
            "goals": [_goal_to_dict(g) for g in goals],
            "total": len(goals),
            "limit": limit,
            "offset": offset,
        }
    finally:
        store.close()


@router.post("/goals", status_code=201)
async def create_goal(body: CreateGoalRequest) -> dict[str, Any]:
    """Create a new goal."""
    store = _get_store()
    try:
        budget = None
        if body.budget:
            budget = GoalBudget(
                max_tasks=body.budget.max_tasks,
                max_turns=body.budget.max_turns,
                max_wall_seconds=body.budget.max_wall_seconds,
                max_tokens=body.budget.max_tokens,
            )
        goal = store.create_goal(
            title=body.title,
            description=body.description,
            priority=body.priority,
            success_criteria=body.success_criteria,
            tags=body.tags,
            budget=budget,
            metadata=body.metadata,
        )
        return _goal_to_dict(goal)
    finally:
        store.close()


@router.get("/goals/{goal_id}")
async def get_goal(goal_id: str) -> dict[str, Any]:
    """Get a single goal by ID."""
    store = _get_store()
    try:
        goal = store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail=f"Goal {goal_id!r} not found")
        return _goal_to_dict(goal)
    finally:
        store.close()


@router.post("/goals/{goal_id}/pause")
async def pause_goal(goal_id: str) -> dict[str, Any]:
    """Pause a running goal."""
    store = _get_store()
    try:
        goal = store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail=f"Goal {goal_id!r} not found")
        try:
            store.update_goal_status(goal_id, GoalStatus.PAUSED)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        updated = store.get_goal(goal_id)
        return _goal_to_dict(updated)
    finally:
        store.close()


@router.post("/goals/{goal_id}/resume")
async def resume_goal(goal_id: str) -> dict[str, Any]:
    """Resume a paused goal."""
    store = _get_store()
    try:
        goal = store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail=f"Goal {goal_id!r} not found")
        try:
            store.update_goal_status(goal_id, GoalStatus.RUNNING)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        updated = store.get_goal(goal_id)
        return _goal_to_dict(updated)
    finally:
        store.close()


@router.post("/goals/{goal_id}/cancel")
async def cancel_goal(goal_id: str) -> dict[str, Any]:
    """Cancel a goal (terminal state)."""
    store = _get_store()
    try:
        goal = store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail=f"Goal {goal_id!r} not found")
        try:
            store.update_goal_status(goal_id, GoalStatus.CANCELLED)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        updated = store.get_goal(goal_id)
        return _goal_to_dict(updated)
    finally:
        store.close()


@router.get("/goals/{goal_id}/tasks")
async def list_goal_tasks(goal_id: str) -> dict[str, Any]:
    """List all tasks for a goal."""
    store = _get_store()
    try:
        goal = store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail=f"Goal {goal_id!r} not found")
        tasks = store.list_tasks(goal_id)
        return {
            "goal_id": goal_id,
            "tasks": [
                {
                    "id": t.task_id,
                    "title": t.title,
                    "description": t.description,
                    "status": t.status.value if hasattr(t.status, "value") else t.status,
                    "sequence": t.sequence,
                    "depends_on": list(t.depends_on) if t.depends_on else [],
                    "created_at": t.created_at.isoformat() if isinstance(t.created_at, datetime) else t.created_at,
                }
                for t in tasks
            ],
        }
    finally:
        store.close()
