"""Shared enum infrastructure: lifecycle protocol and parse helpers.

Status enums across the runtime (`TaskStatus`, `GoalStatus`,
`ApprovalStatus`, `SessionStatus`, ...) all share the notions of "is this
member terminal?" and "is this member actively progressing?". Rather than
forcing a common base class, we declare a runtime-checkable Protocol so any
`StrEnum` can opt in by implementing two classmethod-style instance methods.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable


@runtime_checkable
class Lifecycle(Protocol):
    """Protocol satisfied by every status enum in the runtime.

    Implementers (e.g. `TaskStatus`, `GoalStatus`) provide instance methods
    on each enum member so callers can ask `record.status.is_terminal()`
    without caring which lifecycle they hold.
    """

    def is_terminal(self) -> bool: ...

    def is_active(self) -> bool: ...


def parse_lenient[E: StrEnum](
    enum_cls: type[E],
    value: str | E,
    *,
    default: E | None = None,
) -> E:
    """Parse a raw string (or pre-validated member) into the enum.

    Useful at deserialization boundaries (event store rows, persisted JSON,
    REST request bodies) where the wire value may be a stale string from an
    older runtime. When `default` is supplied, unknown values resolve to it
    rather than raising — callers that need strict parity should omit it.
    """
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError:
        if default is not None:
            return default
        raise
