"""obscura.kairos.goals — File-based goal board for persistent objectives.

Goals live at ``~/.obscura/goals/<slug>.md`` as markdown files with YAML
frontmatter.  The :class:`GoalBoard` provides CRUD and query operations.

Usage::

    board = GoalBoard()
    goal = board.create("Fix auth flow", priority="high",
                        acceptance_criteria=["SSO login works", "Tests pass"])
    board.update(goal.id, progress=60)
    for g in board.active_goals():
        print(g.title, g.progress)
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_GOALS_DIR = Path.home() / ".obscura" / "goals"

_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_VALID_STATUSES = {"draft", "active", "in_progress", "completed", "abandoned"}
_ACTIVE_STATUSES = {"active", "in_progress"}

# Valid lifecycle transitions: current_status → set of allowed next statuses.
_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"active", "abandoned"},
    "active": {"in_progress", "completed", "abandoned"},
    "in_progress": {"completed", "abandoned", "active"},
    "completed": set(),
    "abandoned": {"active"},
}


@dataclass(frozen=True)
class Goal:
    """A single goal parsed from its markdown file."""

    id: str
    title: str
    status: str = "draft"
    priority: str = "medium"
    created: str = ""
    updated: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    tasks: tuple[str, ...] = ()
    progress: int = 0
    last_worked: str | None = None
    body: str = ""
    path: Path = field(default_factory=lambda: Path())

    @property
    def is_active(self) -> bool:
        return self.status in _ACTIVE_STATUSES

    def is_blocked(self, board: GoalBoard | None = None) -> bool:
        """True if any dependency is not completed."""
        if not self.depends_on:
            return False
        if board is None:
            board = GoalBoard()
        for dep_id in self.depends_on:
            dep = board.load(dep_id)
            if dep is None or dep.status != "completed":
                return True
        return False

    @property
    def priority_rank(self) -> int:
        return _PRIORITY_ORDER.get(self.priority, 2)


class GoalBoard:
    """CRUD interface over ``~/.obscura/goals/``."""

    def __init__(self, goals_dir: Path | None = None) -> None:
        self._dir = goals_dir or _GOALS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    # -- Read ----------------------------------------------------------------

    def load_all(self) -> list[Goal]:
        """Parse all ``.md`` files in the goals directory."""
        goals: list[Goal] = []
        for path in sorted(self._dir.glob("*.md")):
            goal = self._parse_file(path)
            if goal is not None:
                goals.append(goal)
        return goals

    def load(self, goal_id: str) -> Goal | None:
        """Load a single goal by ID."""
        path = self._dir / f"{goal_id}.md"
        if path.exists():
            return self._parse_file(path)
        # Prefix match fallback.
        for p in self._dir.glob("*.md"):
            if p.stem.startswith(goal_id):
                return self._parse_file(p)
        return None

    def get_if_newer(self, goal_id: str, since: str) -> Goal | None:
        """Return the in-memory (disk) goal if its 'updated' timestamp is newer than *since*.

        Used by vault_sync conflict detection: if the agent wrote a newer version
        to disk than what the user's file claims, the agent copy might have unsaved
        progress that we should archive before overwriting.

        Args:
            goal_id: The goal slug to look up.
            since: ISO 8601 timestamp to compare against.  If the stored goal's
                ``updated`` field is lexicographically greater (i.e., newer), the
                goal is returned; otherwise ``None`` is returned.
        """
        goal = self.load(goal_id)
        if goal is None:
            return None
        # ISO 8601 strings are lexicographically comparable.
        if goal.updated > since:
            return goal
        return None

    def active_goals(self) -> list[Goal]:
        """Return active/in_progress goals sorted by priority then staleness."""
        active = [g for g in self.load_all() if g.is_active]
        active.sort(key=lambda g: (g.priority_rank, g.updated))
        return active

    def active_summary(self, max_lines: int = 8) -> str:
        """Compact summary for system prompt injection (≤500 chars)."""
        goals = self.active_goals()
        if not goals:
            return ""
        lines: list[str] = []
        for i, g in enumerate(goals[:max_lines], 1):
            prio = g.priority[:4].upper()
            blocked = " BLOCKED" if g.is_blocked(self) else ""
            tasks_note = ""
            if g.tasks:
                tasks_note = f" — {len(g.tasks)} tasks"
            lines.append(
                f"{i}. [{prio}] {g.title} ({g.progress}%){blocked}{tasks_note}"
            )
        remaining = len(goals) - max_lines
        if remaining > 0:
            lines.append(f"   ... and {remaining} more")
        return "\n".join(lines)

    # -- Write ---------------------------------------------------------------

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
        """Create a new goal and write it to disk."""
        goal_id = _slugify(title)
        # Handle collisions.
        if (self._dir / f"{goal_id}.md").exists():
            goal_id = f"{goal_id}-{int(time.time()) % 10000}"

        now = datetime.now(UTC).isoformat()
        goal = Goal(
            id=goal_id,
            title=title,
            status=status,
            priority=priority,
            created=now,
            updated=now,
            acceptance_criteria=tuple(acceptance_criteria or []),
            depends_on=tuple(depends_on or []),
            tasks=(),
            progress=0,
            body=context,
            path=self._dir / f"{goal_id}.md",
        )
        self._write(goal)
        logger.info("Goal created: %s (%s)", goal_id, title)
        return goal

    def update(self, goal_id: str, **fields: Any) -> Goal | None:
        """Update goal fields and write back to disk."""
        goal = self.load(goal_id)
        if goal is None:
            return None

        now = datetime.now(UTC).isoformat()
        updates: dict[str, Any] = {"updated": now}

        for key, val in fields.items():
            if key == "acceptance_criteria" and isinstance(val, list):
                updates[key] = tuple(val)
            elif key == "depends_on" and isinstance(val, list):
                updates[key] = tuple(val)
            elif key == "tasks" and isinstance(val, list):
                updates[key] = tuple(val)
            elif key in {
                "title",
                "status",
                "priority",
                "progress",
                "last_worked",
                "body",
            }:
                updates[key] = val

        # Validate status transition if changing.
        new_status = updates.get("status")
        if new_status and new_status != goal.status:
            allowed = _TRANSITIONS.get(goal.status, set())
            if new_status not in allowed:
                logger.warning(
                    "Invalid transition %s → %s for goal %s",
                    goal.status,
                    new_status,
                    goal_id,
                )
                return None

        # Build updated goal via dataclass replace.
        from dataclasses import replace

        updated = replace(goal, **updates)
        self._write(updated)

        # Auto-decompose: when a goal enters in_progress with acceptance
        # criteria but no linked tasks, push each criterion as a queue task.
        if (
            new_status == "in_progress"
            and goal.status != "in_progress"
            and updated.acceptance_criteria
            and not updated.tasks
        ):
            self._auto_decompose(updated)
            # Reload to pick up linked task IDs.
            updated = self.load(goal_id) or updated

        return updated

    def _auto_decompose(self, goal: Goal) -> None:
        """Push acceptance criteria as tasks into the queue and link them."""
        try:
            from obscura.core.task_queue import TaskQueue

            q = TaskQueue()
            priority = goal.priority_rank * 25  # critical=0, high=25, medium=50, low=75
            task_ids: list[str] = []
            prev_id: str | None = None

            for criterion in goal.acceptance_criteria:
                # Chain tasks sequentially: each blocked by the previous one.
                blocked_by = [prev_id] if prev_id else []
                task_id = q.enqueue(
                    criterion,
                    description=f"Acceptance criterion for goal: {goal.title}",
                    priority=priority,
                    goal_id=goal.id,
                    blocked_by=blocked_by,
                )
                task_ids.append(task_id)
                prev_id = task_id

            # Link all tasks to the goal.
            if task_ids:
                self.update(goal.id, tasks=list(goal.tasks) + task_ids)
                logger.info(
                    "Auto-decomposed goal %s into %d tasks",
                    goal.id,
                    len(task_ids),
                )
        except Exception:
            logger.warning(
                "Auto-decomposition failed for goal %s", goal.id, exc_info=True
            )

    def complete(self, goal_id: str) -> Goal | None:
        """Mark a goal as completed."""
        return self.update(goal_id, status="completed", progress=100)

    def abandon(self, goal_id: str, reason: str = "") -> Goal | None:
        """Mark a goal as abandoned."""
        goal = self.load(goal_id)
        if goal is None:
            return None
        body = goal.body
        if reason:
            body = (
                f"{body}\n\n## Abandoned\n\n{reason}"
                if body
                else f"## Abandoned\n\n{reason}"
            )
        return self.update(goal_id, status="abandoned", body=body)

    def link_task(self, goal_id: str, task_id: str) -> Goal | None:
        """Add a task_id to a goal's linked tasks."""
        goal = self.load(goal_id)
        if goal is None:
            return None
        if task_id in goal.tasks:
            return goal
        return self.update(goal_id, tasks=list(goal.tasks) + [task_id])

    def sync_task_progress(self, goal_id: str) -> Goal | None:
        """Update goal progress based on linked task statuses."""
        goal = self.load(goal_id)
        if goal is None or not goal.tasks:
            return goal
        try:
            from obscura.tools.task_tools import _get_db

            conn = _get_db()
            total = len(goal.tasks)
            completed = 0
            for tid in goal.tasks:
                row = conn.execute(
                    "SELECT status FROM tasks WHERE task_id = ?",
                    (tid,),
                ).fetchone()
                if row and row["status"] == "completed":
                    completed += 1
            conn.close()
            progress = int(completed / total * 100) if total else 0
            return self.update(goal_id, progress=progress)
        except Exception:
            logger.debug("Could not sync task progress for %s", goal_id, exc_info=True)
            return goal

    # -- Internals -----------------------------------------------------------

    def _parse_file(self, path: Path) -> Goal | None:
        """Parse a goal markdown file with YAML frontmatter."""
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:
            return None

        frontmatter, body = _split_frontmatter(raw)
        if frontmatter is None:
            return None

        try:
            data = yaml.safe_load(frontmatter)
        except Exception:
            logger.debug("Invalid YAML in %s", path)
            return None

        if not isinstance(data, dict):
            return None

        def _as_tuple(val: Any) -> tuple[str, ...]:
            if isinstance(val, list):
                return tuple(str(x) for x in val)
            return ()

        return Goal(
            id=data.get("id", path.stem),
            title=data.get("title", path.stem),
            status=data.get("status", "draft"),
            priority=data.get("priority", "medium"),
            created=str(data.get("created", "")),
            updated=str(data.get("updated", "")),
            acceptance_criteria=_as_tuple(data.get("acceptance_criteria")),
            depends_on=_as_tuple(data.get("depends_on")),
            tasks=_as_tuple(data.get("tasks")),
            progress=int(data.get("progress", 0)),
            last_worked=data.get("last_worked"),
            body=body.strip(),
            path=path,
        )

    def _write(self, goal: Goal) -> None:
        """Atomically write a goal to its markdown file."""
        data: dict[str, Any] = {
            "id": goal.id,
            "title": goal.title,
            "status": goal.status,
            "priority": goal.priority,
            "created": goal.created,
            "updated": goal.updated,
            "acceptance_criteria": list(goal.acceptance_criteria),
            "depends_on": list(goal.depends_on),
            "tasks": list(goal.tasks),
            "progress": goal.progress,
            "last_worked": goal.last_worked,
        }
        frontmatter = yaml.dump(data, default_flow_style=False, sort_keys=False)
        body = goal.body or ""
        content = f"---\n{frontmatter}---\n\n{body}\n"

        path = self._dir / f"{goal.id}.md"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(str(tmp), str(path))


# -- Helpers -----------------------------------------------------------------


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug.strip("-")
    return slug[:60] or "untitled"


def _split_frontmatter(raw: str) -> tuple[str | None, str]:
    """Split ``---`` delimited YAML frontmatter from body."""
    if not raw.startswith("---"):
        return None, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return None, raw
    return parts[1], parts[2]
