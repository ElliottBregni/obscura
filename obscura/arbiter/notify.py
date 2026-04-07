"""obscura.arbiter.notify — Fire Arbiter-related supervisor hooks from tools.

Tools call these thin helpers to notify the Arbiter (via supervisor hooks)
when tasks complete or goals transition. If no hook manager is available
(e.g. running outside a supervised session), the calls are no-ops.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Populated by the supervisor at session start.
_hooks: Any = None


def set_hook_manager(hooks: Any) -> None:
    """Called by the supervisor to make the hook manager available to tools."""
    global _hooks  # noqa: PLW0603
    _hooks = hooks


async def fire_task_complete(task: dict[str, Any]) -> dict[str, Any] | None:
    """Fire POST_TASK_COMPLETE hooks. Returns modified context or None if blocked."""
    if _hooks is None:
        return {"task": task}
    try:
        from obscura.core.supervisor.types import SupervisorHookPoint

        ctx: dict[str, Any] | None = {"task": task}
        if hasattr(_hooks, "fire_before"):
            ctx = await _hooks.fire_before(SupervisorHookPoint.POST_TASK_COMPLETE, ctx)
        if ctx is not None and hasattr(_hooks, "fire_after"):
            await _hooks.fire_after(SupervisorHookPoint.POST_TASK_COMPLETE, ctx)
        return ctx
    except Exception:
        logger.debug("fire_task_complete hook failed", exc_info=True)
        return {"task": task}


async def fire_goal_transition(
    goal: dict[str, Any],
    *,
    linked_task_statuses: list[str] | None = None,
) -> dict[str, Any] | None:
    """Fire POST_GOAL_TRANSITION hooks. Returns modified context or None if blocked."""
    if _hooks is None:
        return {"goal": goal}
    try:
        from obscura.core.supervisor.types import SupervisorHookPoint

        ctx: dict[str, Any] | None = {
            "goal": goal,
            "linked_task_statuses": linked_task_statuses or [],
        }
        if hasattr(_hooks, "fire_before"):
            ctx = await _hooks.fire_before(
                SupervisorHookPoint.POST_GOAL_TRANSITION, ctx
            )
        if ctx is not None and hasattr(_hooks, "fire_after"):
            await _hooks.fire_after(SupervisorHookPoint.POST_GOAL_TRANSITION, ctx)
        return ctx
    except Exception:
        logger.debug("fire_goal_transition hook failed", exc_info=True)
        return {"goal": goal}
