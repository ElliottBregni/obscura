"""Pydantic record models for status-bearing lifecycle entities.

Every long-lived row in the runtime that carries a lifecycle status — Task,
Goal, Approval, Worktree, Session, Health, BackgroundTask, TaskQueue — gets
a Pydantic model here. Models compose foundation mixins
(``IdentifiedMixin``, ``TimestampedMixin``, ``StatusedMixin[S]``) so the
status-typed contract is enforced uniformly.

Mutating records (``ApprovalRecord``, ``TaskRecord``, ``GoalRecord``,
``WorktreeEntry``, ``SessionRecord``, ``TaskQueueRecord``,
``BackgroundTaskRecord``) extend ``MutableObscuraModel`` because the
authoritative storage is the SQL row / on-disk JSON, and consumers mutate
the in-memory copy in place. ``HealthReport`` is an immutable
point-in-time snapshot, so it extends ``ObscuraModel``.

Each record exposes ``from_row(row)`` and ``to_row()`` for SQLite
boundaries:

- ``from_row`` parses ``sqlite3.Row`` or ``Mapping`` payloads. Status
  fields use ``parse_lenient`` so older persisted rows with stale enum
  values do not blow up at load time.
- ``to_row`` returns a plain ``dict[str, Any]`` whose values are SQLite-
  bindable (``status.value`` for the enum, ISO strings for datetimes).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Self, cast

from pydantic import Field, field_validator, model_validator

logger = logging.getLogger(__name__)

from obscura.core.enums._base import parse_lenient
from obscura.core.enums.lifecycle import (
    ApprovalStatus,
    BackgroundTaskStatus,
    GoalStatus,
    HealthStatus,
    KairosTaskStatus,
    SessionStatus,
    TaskQueueStatus,
    WorktreeStatus,
)
from obscura.core.models._base import MutableObscuraModel, ObscuraModel
from obscura.core.models._mixins import (
    IdentifiedMixin,
    StatusedMixin,
    TimestampedMixin,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _coerce_dt(value: object) -> datetime:
    """Parse a wire datetime from str / float / datetime into ``datetime``."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            logger.debug("could not parse datetime string %r — using now", value)
            return datetime.now(UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _row_get(
    row: sqlite3.Row | Mapping[str, Any], key: str, default: Any = None
) -> Any:
    """Mapping-style ``.get`` that also works on ``sqlite3.Row``."""
    if isinstance(row, sqlite3.Row):
        # sqlite3.Row supports membership via __contains__ on keys() but not
        # __contains__ directly, so we explicitly check key presence.
        if key in row.keys():  # noqa: SIM118
            return row[key]
        return default
    return row.get(key, default)


def _decode_json_list(value: object) -> list[Any]:
    """JSON-decode a column expected to hold a list."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return list(cast("list[Any]", value))
    if isinstance(value, str):
        try:
            parsed: object = json.loads(value)
        except json.JSONDecodeError:
            logger.debug("could not JSON-decode list column: %r", value)
            return []
        return list(cast("list[Any]", parsed)) if isinstance(parsed, list) else []
    return []


def _decode_json_dict(value: object) -> dict[str, Any]:
    """JSON-decode a column expected to hold an object."""
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return dict(cast("dict[str, Any]", value))
    if isinstance(value, str):
        try:
            parsed: object = json.loads(value)
        except json.JSONDecodeError:
            logger.debug("could not JSON-decode dict column: %r", value)
            return {}
        return dict(cast("dict[str, Any]", parsed)) if isinstance(parsed, dict) else {}
    return {}


def _str_list(values: list[Any]) -> tuple[str, ...]:
    """Coerce a heterogeneous list to a tuple of strings."""
    return tuple(str(v) for v in values)


# ---------------------------------------------------------------------------
# ApprovalRecord
# ---------------------------------------------------------------------------


class ApprovalRecord(
    MutableObscuraModel,
    IdentifiedMixin,
    TimestampedMixin,
    StatusedMixin[ApprovalStatus],
):
    """A pending or resolved tool-confirmation request.

    Mirrors the in-memory ``ToolApprovalRequest`` row — ``id`` corresponds to
    the public ``approval_id``. ``resolved_at`` is the user-decision timestamp
    (distinct from ``status_changed_at`` which the mixin tracks for any
    transition).
    """

    user_id: str
    agent_id: str
    tool_use_id: str
    tool_name: str
    tool_input: Mapping[str, Any] = Field(default_factory=dict)
    resolved_at: datetime | None = None
    decision_reason: str | None = None

    @property
    def approval_id(self) -> str:
        """Legacy alias for the historical dataclass field name."""
        return self.id

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> Self:
        status_raw = _row_get(row, "status", ApprovalStatus.PENDING.value)
        status = parse_lenient(
            ApprovalStatus, str(status_raw), default=ApprovalStatus.PENDING
        )
        created = _coerce_dt(_row_get(row, "created_at"))
        updated = _coerce_dt(_row_get(row, "updated_at", created))
        status_changed = _coerce_dt(_row_get(row, "status_changed_at", updated))
        resolved_raw = _row_get(row, "resolved_at")
        resolved = _coerce_dt(resolved_raw) if resolved_raw else None
        return cls(
            id=str(_row_get(row, "approval_id") or _row_get(row, "id", "")),
            status=status,
            status_changed_at=status_changed,
            created_at=created,
            updated_at=updated,
            user_id=str(_row_get(row, "user_id", "")),
            agent_id=str(_row_get(row, "agent_id", "")),
            tool_use_id=str(_row_get(row, "tool_use_id", "")),
            tool_name=str(_row_get(row, "tool_name", "")),
            tool_input=_decode_json_dict(_row_get(row, "tool_input")),
            resolved_at=resolved,
            decision_reason=_row_get(row, "decision_reason"),
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "approval_id": self.id,
            "status": self.status.value,
            "status_changed_at": self.status_changed_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "tool_use_id": self.tool_use_id,
            "tool_name": self.tool_name,
            "tool_input": dict(self.tool_input),
            "resolved_at": self.resolved_at.isoformat()
            if self.resolved_at is not None
            else None,
            "decision_reason": self.decision_reason,
        }

    def to_dict(self) -> dict[str, Any]:
        """Public HTTP wire shape — historical key set, ISO-string timestamps.

        Distinct from ``to_row()`` because route consumers don't see
        ``status_changed_at`` / ``updated_at``.
        """
        return {
            "approval_id": self.id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "tool_use_id": self.tool_use_id,
            "tool_name": self.tool_name,
            "tool_input": dict(self.tool_input),
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat()
            if self.resolved_at is not None
            else None,
            "decision_reason": self.decision_reason,
        }


# ---------------------------------------------------------------------------
# TaskRecord
# ---------------------------------------------------------------------------


class TaskRecord(
    MutableObscuraModel,
    IdentifiedMixin,
    TimestampedMixin,
    StatusedMixin[KairosTaskStatus],
):
    """A Kairos-style task record for the agent-facing task tooling.

    The Kairos lifecycle (``running`` / ``succeeded`` / ``retrying`` / …) is
    used here even though the underlying SQLite column also stores
    queue-only values (``pending`` / ``in_progress`` / ``completed`` / …).
    ``from_row`` falls back to ``KairosTaskStatus.PENDING`` on values that
    are not part of the Kairos vocabulary so historical rows still parse;
    callers that need the raw queue lifecycle should use
    :class:`TaskQueueRecord` instead.
    """

    subject: str
    description: str = ""
    owner: str = ""
    active_form: str = ""
    priority: int = 50
    goal_id: str = ""
    blocks: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = Field(default_factory=dict)
    output: str = ""
    error: str = ""
    project_root: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> Self:
        status_raw = _row_get(row, "status", KairosTaskStatus.PENDING.value)
        status = parse_lenient(
            KairosTaskStatus, str(status_raw), default=KairosTaskStatus.PENDING
        )
        created = _coerce_dt(_row_get(row, "created_at"))
        updated = _coerce_dt(_row_get(row, "updated_at", created))
        status_changed = _coerce_dt(_row_get(row, "status_changed_at", updated))
        return cls(
            id=str(_row_get(row, "task_id") or _row_get(row, "id", "")),
            status=status,
            status_changed_at=status_changed,
            created_at=created,
            updated_at=updated,
            subject=str(_row_get(row, "subject", "")),
            description=str(_row_get(row, "description", "") or ""),
            owner=str(_row_get(row, "owner", "") or ""),
            active_form=str(_row_get(row, "active_form", "") or ""),
            priority=int(_row_get(row, "priority", 50) or 50),
            goal_id=str(_row_get(row, "goal_id", "") or ""),
            blocks=_str_list(_decode_json_list(_row_get(row, "blocks"))),
            blocked_by=_str_list(_decode_json_list(_row_get(row, "blocked_by"))),
            metadata=_decode_json_dict(_row_get(row, "metadata")),
            output=str(_row_get(row, "output", "") or ""),
            error=str(_row_get(row, "error", "") or ""),
            project_root=str(_row_get(row, "project_root", "") or ""),
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "status": self.status.value,
            "status_changed_at": self.status_changed_at.isoformat(),
            "created_at": self.created_at.timestamp(),
            "updated_at": self.updated_at.timestamp(),
            "subject": self.subject,
            "description": self.description,
            "owner": self.owner,
            "active_form": self.active_form,
            "priority": self.priority,
            "goal_id": self.goal_id,
            "blocks": json.dumps(list(self.blocks)),
            "blocked_by": json.dumps(list(self.blocked_by)),
            "metadata": json.dumps(dict(self.metadata)),
            "output": self.output,
            "error": self.error,
            "project_root": self.project_root,
        }


# ---------------------------------------------------------------------------
# GoalRecord
# ---------------------------------------------------------------------------


class GoalRecord(
    MutableObscuraModel,
    IdentifiedMixin,
    TimestampedMixin,
    StatusedMixin[GoalStatus],
):
    """A Kairos goal as exposed by ``tools/goal_tools.py``.

    The on-disk source of truth is a markdown file with YAML frontmatter
    (``~/.obscura/goals/<slug>.md``). This model is the typed view used by
    the goal-tool callsites; it does not own persistence — the
    :class:`obscura.kairos.goals.GoalBoard` continues to read and write the
    files.
    """

    title: str
    priority: str = "medium"
    progress: int = 0
    acceptance_criteria: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    tasks: tuple[str, ...] = ()
    body: str = ""
    last_worked: str | None = None
    project_root: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> Self:
        status_raw = _row_get(row, "status", GoalStatus.PENDING.value)
        status = parse_lenient(GoalStatus, str(status_raw), default=GoalStatus.PENDING)
        created = _coerce_dt(_row_get(row, "created_at") or _row_get(row, "created"))
        updated = _coerce_dt(
            _row_get(row, "updated_at") or _row_get(row, "updated", created),
        )
        status_changed = _coerce_dt(_row_get(row, "status_changed_at", updated))
        return cls(
            id=str(_row_get(row, "id") or _row_get(row, "goal_id", "")),
            status=status,
            status_changed_at=status_changed,
            created_at=created,
            updated_at=updated,
            title=str(_row_get(row, "title", "") or ""),
            priority=str(_row_get(row, "priority", "medium") or "medium"),
            progress=int(_row_get(row, "progress", 0) or 0),
            acceptance_criteria=_str_list(
                _decode_json_list(_row_get(row, "acceptance_criteria"))
            ),
            depends_on=_str_list(_decode_json_list(_row_get(row, "depends_on"))),
            tasks=_str_list(_decode_json_list(_row_get(row, "tasks"))),
            body=str(_row_get(row, "body", "") or ""),
            last_worked=_row_get(row, "last_worked"),
            project_root=str(_row_get(row, "project_root", "") or ""),
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "status_changed_at": self.status_changed_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "title": self.title,
            "priority": self.priority,
            "progress": self.progress,
            "acceptance_criteria": list(self.acceptance_criteria),
            "depends_on": list(self.depends_on),
            "tasks": list(self.tasks),
            "body": self.body,
            "last_worked": self.last_worked,
            "project_root": self.project_root,
        }


# ---------------------------------------------------------------------------
# WorktreeEntry
# ---------------------------------------------------------------------------


class WorktreeEntry(
    MutableObscuraModel,
    TimestampedMixin,
    StatusedMixin[WorktreeStatus],
):
    """A registered git worktree checkout.

    The slug is the natural identifier (collisions are rejected at
    registration time), so this record does not include
    :class:`IdentifiedMixin`. Persistence is JSON in
    ``~/.obscura/worktrees/registry.json``.

    The on-disk JSON stores Unix timestamps (floats), so the timestamp
    fields accept ``float`` / ``int`` in addition to ``datetime`` —
    historical callsites pass ``time.time()`` directly. ``updated_at``
    and ``status_changed_at`` default to ``created_at`` when omitted.
    """

    # Override the mixin defaults so historical positional construction
    # (`WorktreeEntry(..., created_at=time.time())`) keeps working.
    updated_at: datetime = Field(default=None)  # type: ignore[assignment]
    status_changed_at: datetime = Field(default=None)  # type: ignore[assignment]
    status: WorktreeStatus = WorktreeStatus.ACTIVE
    slug: str
    repo_root: str
    repo_hash: str
    worktree_path: str
    branch: str
    original_cwd: str
    owner: str
    pid: int
    agent_name: str = ""

    @field_validator("created_at", "updated_at", "status_changed_at", mode="before")
    @classmethod
    def _coerce_timestamps(cls, value: object) -> object:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=UTC)
        return value

    @model_validator(mode="after")
    def _backfill_timestamps(self) -> Self:
        if self.updated_at is None:  # type: ignore[truthy-bool]
            object.__setattr__(self, "updated_at", self.created_at)
        if self.status_changed_at is None:  # type: ignore[truthy-bool]
            object.__setattr__(self, "status_changed_at", self.created_at)
        return self

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> Self:
        status_raw = _row_get(row, "status", WorktreeStatus.ACTIVE.value)
        status = parse_lenient(
            WorktreeStatus, str(status_raw), default=WorktreeStatus.ACTIVE
        )
        created = _coerce_dt(_row_get(row, "created_at"))
        updated = _coerce_dt(_row_get(row, "updated_at", created))
        status_changed = _coerce_dt(_row_get(row, "status_changed_at", updated))
        return cls(
            slug=str(_row_get(row, "slug", "")),
            status=status,
            status_changed_at=status_changed,
            created_at=created,
            updated_at=updated,
            repo_root=str(_row_get(row, "repo_root", "")),
            repo_hash=str(_row_get(row, "repo_hash", "")),
            worktree_path=str(_row_get(row, "worktree_path", "")),
            branch=str(_row_get(row, "branch", "")),
            original_cwd=str(_row_get(row, "original_cwd", "")),
            owner=str(_row_get(row, "owner", "tool")),
            pid=int(_row_get(row, "pid", 0) or 0),
            agent_name=str(_row_get(row, "agent_name", "") or ""),
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "status": self.status.value,
            "status_changed_at": self.status_changed_at.isoformat(),
            "created_at": self.created_at.timestamp(),
            "updated_at": self.updated_at.timestamp(),
            "repo_root": self.repo_root,
            "repo_hash": self.repo_hash,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "original_cwd": self.original_cwd,
            "owner": self.owner,
            "pid": self.pid,
            "agent_name": self.agent_name,
        }


# ---------------------------------------------------------------------------
# SessionRecord
# ---------------------------------------------------------------------------


class SessionRecord(
    MutableObscuraModel,
    IdentifiedMixin,
    TimestampedMixin,
    StatusedMixin[SessionStatus],
):
    """A durable agent session row in the event store.

    ``status_changed_at`` defaults to ``updated_at`` (or ``created_at``)
    when omitted by callsites that predate the StatusedMixin contract —
    notably the Postgres backing store at
    ``obscura/core/postgres_event_store.py`` which constructs records
    without an explicit transition timestamp.
    """

    status_changed_at: datetime = Field(default=None)  # type: ignore[assignment]
    backend: str = ""
    model: str = ""
    active_agent: str = ""
    source: str = "live"
    parent_session_id: str = ""
    project: str = ""
    summary: str = ""
    message_count: int = 0
    metadata: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _backfill_status_changed_at(self) -> Self:
        if self.status_changed_at is None:  # type: ignore[truthy-bool]
            object.__setattr__(self, "status_changed_at", self.updated_at)
        return self

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> Self:
        status_raw = _row_get(row, "status", SessionStatus.RUNNING.value)
        status = parse_lenient(
            SessionStatus, str(status_raw), default=SessionStatus.RUNNING
        )
        created = _coerce_dt(_row_get(row, "created_at"))
        updated = _coerce_dt(_row_get(row, "updated_at", created))
        status_changed = _coerce_dt(_row_get(row, "status_changed_at", updated))
        return cls(
            id=str(_row_get(row, "id", "")),
            status=status,
            status_changed_at=status_changed,
            created_at=created,
            updated_at=updated,
            backend=str(_row_get(row, "backend", "") or ""),
            model=str(_row_get(row, "model", "") or ""),
            active_agent=str(_row_get(row, "active_agent", "") or ""),
            source=str(_row_get(row, "source", "live") or "live"),
            parent_session_id=str(_row_get(row, "parent_session_id", "") or ""),
            project=str(_row_get(row, "project", "") or ""),
            summary=str(_row_get(row, "summary", "") or ""),
            message_count=int(_row_get(row, "message_count", 0) or 0),
            metadata=_decode_json_dict(_row_get(row, "metadata")),
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "status_changed_at": self.status_changed_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "backend": self.backend,
            "model": self.model,
            "active_agent": self.active_agent,
            "source": self.source,
            "parent_session_id": self.parent_session_id,
            "project": self.project,
            "summary": self.summary,
            "message_count": self.message_count,
            "metadata": json.dumps(dict(self.metadata), default=str),
        }


# ---------------------------------------------------------------------------
# HealthReport
# ---------------------------------------------------------------------------


class HealthReport(ObscuraModel, StatusedMixin[HealthStatus]):
    """A point-in-time health-check result. Frozen — snapshots do not mutate."""

    name: str
    message: str

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> Self:
        status_raw = _row_get(row, "status", HealthStatus.OK.value)
        status = parse_lenient(HealthStatus, str(status_raw), default=HealthStatus.OK)
        status_changed = _coerce_dt(_row_get(row, "status_changed_at"))
        return cls(
            status=status,
            status_changed_at=status_changed,
            name=str(_row_get(row, "name", "")),
            message=str(_row_get(row, "message", "") or ""),
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "status_changed_at": self.status_changed_at.isoformat(),
            "name": self.name,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# TaskQueueRecord
# ---------------------------------------------------------------------------


class TaskQueueRecord(
    MutableObscuraModel,
    IdentifiedMixin,
    TimestampedMixin,
    StatusedMixin[TaskQueueStatus],
):
    """A row of the durable SQLite task queue (``~/.obscura/tasks.db``).

    Same physical row as :class:`TaskRecord`, but typed against the queue's
    own narrower lifecycle (``pending`` / ``in_progress`` / ``completed`` /
    ``failed`` / ``deleted``). Use this for queue-internal logic; use
    :class:`TaskRecord` for the agent-facing surface that wants the richer
    Kairos lifecycle.
    """

    subject: str
    description: str = ""
    owner: str = ""
    active_form: str = ""
    priority: int = 50
    goal_id: str = ""
    claimed_by: str = ""
    claimed_at: float = 0.0
    last_heartbeat: float = 0.0
    run_after: float = 0.0
    max_retries: int = 3
    retry_count: int = 0
    blocks: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = Field(default_factory=dict)
    output: str = ""
    error: str = ""
    project_root: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> Self:
        status_raw = _row_get(row, "status", TaskQueueStatus.PENDING.value)
        status = parse_lenient(
            TaskQueueStatus, str(status_raw), default=TaskQueueStatus.PENDING
        )
        created = _coerce_dt(_row_get(row, "created_at"))
        updated = _coerce_dt(_row_get(row, "updated_at", created))
        status_changed = _coerce_dt(_row_get(row, "status_changed_at", updated))
        return cls(
            id=str(_row_get(row, "task_id") or _row_get(row, "id", "")),
            status=status,
            status_changed_at=status_changed,
            created_at=created,
            updated_at=updated,
            subject=str(_row_get(row, "subject", "")),
            description=str(_row_get(row, "description", "") or ""),
            owner=str(_row_get(row, "owner", "") or ""),
            active_form=str(_row_get(row, "active_form", "") or ""),
            priority=int(_row_get(row, "priority", 50) or 50),
            goal_id=str(_row_get(row, "goal_id", "") or ""),
            claimed_by=str(_row_get(row, "claimed_by", "") or ""),
            claimed_at=float(_row_get(row, "claimed_at", 0.0) or 0.0),
            last_heartbeat=float(_row_get(row, "last_heartbeat", 0.0) or 0.0),
            run_after=float(_row_get(row, "run_after", 0.0) or 0.0),
            max_retries=int(_row_get(row, "max_retries", 3) or 3),
            retry_count=int(_row_get(row, "retry_count", 0) or 0),
            blocks=_str_list(_decode_json_list(_row_get(row, "blocks"))),
            blocked_by=_str_list(_decode_json_list(_row_get(row, "blocked_by"))),
            metadata=_decode_json_dict(_row_get(row, "metadata")),
            output=str(_row_get(row, "output", "") or ""),
            error=str(_row_get(row, "error", "") or ""),
            project_root=str(_row_get(row, "project_root", "") or ""),
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "status": self.status.value,
            "status_changed_at": self.status_changed_at.isoformat(),
            "created_at": self.created_at.timestamp(),
            "updated_at": self.updated_at.timestamp(),
            "subject": self.subject,
            "description": self.description,
            "owner": self.owner,
            "active_form": self.active_form,
            "priority": self.priority,
            "goal_id": self.goal_id,
            "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at,
            "last_heartbeat": self.last_heartbeat,
            "run_after": self.run_after,
            "max_retries": self.max_retries,
            "retry_count": self.retry_count,
            "blocks": json.dumps(list(self.blocks)),
            "blocked_by": json.dumps(list(self.blocked_by)),
            "metadata": json.dumps(dict(self.metadata)),
            "output": self.output,
            "error": self.error,
            "project_root": self.project_root,
        }


# ---------------------------------------------------------------------------
# BackgroundTaskRecord
# ---------------------------------------------------------------------------


class BackgroundTaskRecord(
    MutableObscuraModel,
    IdentifiedMixin,
    StatusedMixin[BackgroundTaskStatus],
):
    """A long-running shell process tracked by the background-task manager.

    State is in-memory only (no SQLite backing) — ``from_row`` exists for
    parity with the other records in this module so dict-shaped snapshots
    (e.g. ``task_output`` JSON responses) can round-trip.
    """

    status_changed_at: datetime = Field(default=None)  # type: ignore[assignment]
    command: str
    cwd: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    started_at: float = 0.0
    completed_at: float | None = None

    @property
    def task_id(self) -> str:
        """Legacy alias for the historical dataclass field name."""
        return self.id

    @model_validator(mode="after")
    def _backfill_status_changed_at(self) -> Self:
        if self.status_changed_at is None:  # type: ignore[truthy-bool]
            ts = (
                datetime.fromtimestamp(self.started_at, tz=UTC)
                if self.started_at
                else datetime.now(UTC)
            )
            object.__setattr__(self, "status_changed_at", ts)
        return self

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> Self:
        status_raw = _row_get(row, "status", BackgroundTaskStatus.RUNNING.value)
        status = parse_lenient(
            BackgroundTaskStatus,
            str(status_raw),
            default=BackgroundTaskStatus.RUNNING,
        )
        started = float(_row_get(row, "started_at", 0.0) or 0.0)
        completed_raw = _row_get(row, "completed_at")
        completed = float(completed_raw) if completed_raw else None
        status_changed = (
            _coerce_dt(
                _row_get(row, "status_changed_at"),
            )
            if _row_get(row, "status_changed_at")
            else _coerce_dt(started)
        )
        exit_code_raw = _row_get(row, "exit_code")
        return cls(
            id=str(_row_get(row, "task_id") or _row_get(row, "id", "")),
            status=status,
            status_changed_at=status_changed,
            command=str(_row_get(row, "command", "")),
            cwd=str(_row_get(row, "cwd", "") or ""),
            stdout=str(_row_get(row, "stdout", "") or ""),
            stderr=str(_row_get(row, "stderr", "") or ""),
            exit_code=int(exit_code_raw) if exit_code_raw is not None else None,
            started_at=started,
            completed_at=completed,
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "status": self.status.value,
            "status_changed_at": self.status_changed_at.isoformat(),
            "command": self.command,
            "cwd": self.cwd,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


__all__ = [
    "ApprovalRecord",
    "BackgroundTaskRecord",
    "GoalRecord",
    "HealthReport",
    "SessionRecord",
    "TaskQueueRecord",
    "TaskRecord",
    "WorktreeEntry",
]
