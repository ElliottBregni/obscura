"""Pydantic mixins composed via multiple inheritance.

Pydantic v2 picks up fields declared on every `BaseModel` ancestor under
multiple inheritance, so these mixins compose cleanly:

    class TaskRecord(IdentifiedMixin, TimestampedMixin, StatusedMixin[TaskStatus]):
        ...

`StatusedMixin` is generic over the status enum type — one mixin handles
every lifecycle (Task, Goal, Approval, Worktree, Session, Health). All
mixins are themselves `BaseModel` subclasses; do not subclass them as plain
classes or Pydantic will not see their fields.

`IdentifiedMixin.id` is typed as `str` (rather than `ULID`) to keep the
ULID dependency optional. Callers should produce ULID-encoded strings when
populating it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class TimestampedMixin(BaseModel):
    created_at: datetime
    updated_at: datetime


class IdentifiedMixin(BaseModel):
    id: str


class MetadataMixin(BaseModel):
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, str] = Field(default_factory=dict)


class StatusedMixin[S: StrEnum](BaseModel):
    status: S
    status_changed_at: datetime
