"""Factory for the goal-board repository.

Single backend today (markdown files); the factory exists so consumers
depend on the :class:`GoalRepo` Protocol rather than the concrete
``GoalBoard`` class. When a SQL backend lands later, callers don't
change.
"""

from __future__ import annotations

import logging
from pathlib import Path

from obscura.data.goals.protocol import GoalRepo
from obscura.kairos.goals import GoalBoard

logger = logging.getLogger(__name__)


def get_goal_repo(goals_dir: Path | None = None) -> GoalRepo:
    """Return a :class:`GoalRepo` for the configured store.

    Args:
        goals_dir: Override the default ``~/.obscura/goals/``. Useful in
            tests or when running multiple isolated stores.
    """
    return GoalBoard(goals_dir=goals_dir)
