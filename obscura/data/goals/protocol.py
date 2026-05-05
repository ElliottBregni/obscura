"""Protocol for the goal-board repository.

Goal board today is a markdown-file store at ``~/.obscura/goals/<slug>.md``.
The Protocol exposes the verbs callers actually use; backend-specific
extras (auto-decompose into queue tasks, etc.) live on the impl class
where they belong rather than leaking into every consumer.

The :class:`Goal` dataclass is re-exported from
:mod:`obscura.kairos.goals` because that's where it currently lives;
once Phase 4 cleanup folds the implementation into
``obscura/data/goals/markdown.py``, the dataclass moves with it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from obscura.kairos.goals import Goal as Goal


@runtime_checkable
class GoalRepo(Protocol):
    """Backend-agnostic interface for the goal board."""

    def load(self, goal_id: str) -> Goal | None:
        """Load a single goal by id (with prefix-match fallback)."""
        ...

    def load_all(self) -> list[Goal]:
        """Return every goal in the store."""
        ...

    def active_goals(self, *, project_root: str | None = None) -> list[Goal]:
        """Return active/in-progress goals, sorted by priority + staleness."""
        ...

    def active_summary(self, max_lines: int = 8) -> str:
        """Compact summary string for system-prompt injection."""
        ...

    def get_if_newer(self, goal_id: str, since: str) -> Goal | None:
        """Return the goal iff its `updated` timestamp is newer than *since*."""
        ...

    def create(
        self,
        title: str,
        *,
        priority: str = "medium",
        context: str = "",
        acceptance_criteria: list[str] | None = None,
        depends_on: list[str] | None = None,
        status: str = "active",
    ) -> Goal:
        """Create a new goal and persist it."""
        ...

    def update(self, goal_id: str, **fields: Any) -> Goal | None:  # noqa: ANN401  # mirrors GoalBoard.update signature
        """Patch goal fields and persist."""
        ...
