"""obscura.core.supervisor.profile_goal_hook — Inject goal board + user profile into every agent.

Registers a PRE_BUILD_CONTEXT hook that adds two system prompt sections:

1. **Active Goals** — compact summary from :class:`GoalBoard`
2. **User Profile** — decayed-weighted summary from :class:`ProfileBuilder`

This ensures every supervised agent (not just KAIROS) sees the user's
goals and profile context.

Usage::

    from obscura.core.supervisor.profile_goal_hook import register_profile_goal_hooks

    register_profile_goal_hooks(hooks, user=user)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from obscura.core.supervisor.types import SupervisorHookPoint

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser
    from obscura.core.supervisor.session_hooks import SessionHookManager

logger = logging.getLogger(__name__)


def register_profile_goal_hooks(
    hooks: SessionHookManager,
    *,
    user: AuthenticatedUser | None = None,
) -> None:
    """Register goal board and user profile hooks on a supervisor.

    Adds a PRE_BUILD_CONTEXT before-hook that injects goal + profile
    summaries into ``context["_goal_context"]`` and
    ``context["_profile_context"]`` for the prompt assembler to pick up.

    Parameters
    ----------
    hooks:
        The supervisor's SessionHookManager.
    user:
        Authenticated user for profile lookup. If None, profile injection
        is skipped.
    """

    async def _inject_goal_and_profile(context: dict[str, Any]) -> None:
        """Inject goal summary and profile summary into build context."""
        # -- Goals (always available, no user dependency) ----------------------
        try:
            from obscura.kairos.goals import GoalBoard

            board = GoalBoard()
            goal_summary = board.active_summary()
            if goal_summary:
                context["_goal_context"] = "## Active Goals\n" + goal_summary
                logger.debug("Injected %d-char goal summary", len(goal_summary))
        except Exception:
            logger.debug("Could not inject goal context", exc_info=True)

        # -- Profile (requires user + vector store) ----------------------------
        if user is None:
            return

        try:
            from obscura.profile.builder import ProfileBuilder
            from obscura.profile.store import ProfileStore

            profile_store = ProfileStore.for_user(user)
            builder = ProfileBuilder()
            profile_summary = builder.build_summary(profile_store)
            if profile_summary:
                context["_profile_context"] = "## User Profile\n" + profile_summary
                logger.debug("Injected %d-char profile summary", len(profile_summary))
        except Exception:
            logger.debug("Could not inject profile context", exc_info=True)

        # -- Arbiter recent verdicts (self-awareness) --------------------------
        try:
            from obscura.arbiter.hooks import get_engine

            engine = get_engine()
            if engine is not None:
                summary = engine.get_recent_verdict_summary(limit=3)
                if summary:
                    context["_arbiter_context"] = (
                        "## Recent Arbiter Verdicts\n" + summary
                    )
                    logger.debug("Injected arbiter verdict summary")
        except Exception:
            logger.debug("Could not inject arbiter context", exc_info=True)

    hooks.register(
        SupervisorHookPoint.PRE_BUILD_CONTEXT,
        "before",
        "profile_goal_inject",
        _inject_goal_and_profile,
        persist=False,
    )

    logger.info("Registered profile + goal hooks")


__all__ = ["register_profile_goal_hooks"]
