"""Routes: Kairos autonomous goal runtime."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from obscura.auth.models import AuthenticatedUser
from obscura.auth.rbac import AGENT_READ_ROLES, AGENT_WRITE_ROLES, require_any_role
from obscura.core.kairos import (
    Goal,
    GoalBudget,
    GoalNotFoundError,
    GoalStatus,
    GoalStore,
)
from obscura.core.paths import resolve_obscura_home

router = APIRouter(prefix="/api/v1", tags=["goals"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_store() -> GoalStore:
    """Return a fresh GoalStore bound to the default kairos.db path."""
    db_path = resolve_obscura_home() / "kairos.db"
    return GoalStore(str(db_path))


def _safe_get_goal(store: GoalStore, goal_id: str) -> Goal | None:
    """Wrap GoalStore.get_goal (which raises) with a None-on-missing API."""
    try:
        return store.get_goal(goal_id)
    except GoalNotFoundError:
        return None


def _goal_to_dict(goal: Goal) -> dict[str, Any]:
    """Serialize a Goal dataclass to a JSON-safe dict."""
    return {
        "id": goal.goal_id,
        "title": goal.title,
        "description": goal.description,
        "status": goal.status.value,
        "success_criteria": list(goal.success_criteria),
        "tags": list(goal.tags),
        "budget": {
            "max_tasks": goal.budget.max_tasks,
            "max_turns": goal.budget.max_turns,
            "max_wall_seconds": goal.budget.max_wall_seconds,
            "max_tokens": goal.budget.max_tokens,
        },
        "created_at": goal.created_at.isoformat(),
        "started_at": goal.started_at.isoformat() if goal.started_at else None,
        "completed_at": goal.completed_at.isoformat() if goal.completed_at else None,
        "metadata": dict(goal.metadata),
    }


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class BudgetRequest(BaseModel):
    max_tasks: int = Field(0, ge=0, description="0 = unlimited")
    max_turns: int = Field(0, ge=0)
    max_wall_seconds: float = Field(0.0, ge=0.0)
    max_tokens: int = Field(0, ge=0)


def _empty_str_list() -> list[str]:
    return []


def _empty_any_dict() -> dict[str, Any]:
    return {}


class CreateGoalRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    description: str = Field("", max_length=4096)
    priority: int = Field(50, ge=0, le=100)
    success_criteria: list[str] = Field(default_factory=_empty_str_list)
    tags: list[str] = Field(default_factory=_empty_str_list)
    budget: BudgetRequest | None = None
    metadata: dict[str, Any] = Field(default_factory=_empty_any_dict)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/goals")
async def list_goals(
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
    status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List goals, optionally filtered by status."""
    del user  # auth-only side effect
    store = _get_store()
    try:
        status_filter = GoalStatus(status) if status else None
        goals = store.list_goals(status=status_filter, limit=limit)
        return {
            "goals": [_goal_to_dict(g) for g in goals],
            "total": len(goals),
            "limit": limit,
        }
    finally:
        store.close()


@router.post("/goals", status_code=201)
async def create_goal(
    body: CreateGoalRequest,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_WRITE_ROLES))],
) -> dict[str, Any]:
    """Create a new goal."""
    store = _get_store()
    try:
        budget = GoalBudget()
        if body.budget is not None:
            budget = GoalBudget(
                max_tasks=body.budget.max_tasks,
                max_turns=body.budget.max_turns,
                max_wall_seconds=body.budget.max_wall_seconds,
                max_tokens=body.budget.max_tokens,
            )
        goal = Goal(
            goal_id=uuid.uuid4().hex,
            title=body.title,
            description=body.description,
            success_criteria=tuple(body.success_criteria),
            owner_id=user.user_id,
            budget=budget,
            tags=tuple(body.tags),
            metadata=dict(body.metadata),
            created_at=datetime.now(UTC),
        )
        store.create_goal(goal)
        return _goal_to_dict(goal)
    finally:
        store.close()


@router.get("/goals/{goal_id}")
async def get_goal(
    goal_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
) -> dict[str, Any]:
    """Get a single goal by ID."""
    del user
    store = _get_store()
    try:
        goal = _safe_get_goal(store, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail=f"Goal {goal_id!r} not found")
        return _goal_to_dict(goal)
    finally:
        store.close()


@router.post("/goals/{goal_id}/pause")
async def pause_goal(
    goal_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_WRITE_ROLES))],
) -> dict[str, Any]:
    """Pause a running goal."""
    del user
    store = _get_store()
    try:
        goal = _safe_get_goal(store, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail=f"Goal {goal_id!r} not found")
        try:
            store.update_goal_status(goal_id, GoalStatus.PAUSED)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        updated = _safe_get_goal(store, goal_id)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Goal {goal_id!r} not found")
        return _goal_to_dict(updated)
    finally:
        store.close()


@router.post("/goals/{goal_id}/resume")
async def resume_goal(
    goal_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_WRITE_ROLES))],
) -> dict[str, Any]:
    """Resume a paused goal."""
    del user
    store = _get_store()
    try:
        goal = _safe_get_goal(store, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail=f"Goal {goal_id!r} not found")
        try:
            store.update_goal_status(goal_id, GoalStatus.ACTIVE)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        updated = _safe_get_goal(store, goal_id)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Goal {goal_id!r} not found")
        return _goal_to_dict(updated)
    finally:
        store.close()


@router.post("/goals/{goal_id}/cancel")
async def cancel_goal(
    goal_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_WRITE_ROLES))],
) -> dict[str, Any]:
    """Cancel a goal (terminal state)."""
    del user
    store = _get_store()
    try:
        goal = _safe_get_goal(store, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail=f"Goal {goal_id!r} not found")
        try:
            store.update_goal_status(goal_id, GoalStatus.CANCELLED)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        updated = _safe_get_goal(store, goal_id)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Goal {goal_id!r} not found")
        return _goal_to_dict(updated)
    finally:
        store.close()


@router.get("/goals/{goal_id}/tasks")
async def list_goal_tasks(
    goal_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
) -> dict[str, Any]:
    """List all tasks for a goal's active plan."""
    del user
    store = _get_store()
    try:
        goal = _safe_get_goal(store, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail=f"Goal {goal_id!r} not found")
        plan = store.get_active_plan(goal_id)
        if plan is None:
            return {"goal_id": goal_id, "tasks": []}
        tasks = store.list_tasks(plan.plan_id)
        return {
            "goal_id": goal_id,
            "tasks": [
                {
                    "id": t.task_id,
                    "title": t.title,
                    "description": t.description,
                    "status": t.status.value,
                    "order_index": t.order_index,
                    "depends_on": list(t.depends_on),
                    "created_at": t.created_at.isoformat(),
                }
                for t in tasks
            ],
        }
    finally:
        store.close()
