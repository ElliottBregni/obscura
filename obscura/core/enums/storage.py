"""Storage / memory-domain enums.

Promotes the loose `Literal[...]` aliases scattered across
`memory/events.py` and `vector_memory/vector_memory_filters.py` into typed
`StrEnum`s, plus introduces `DBBackendType`, `MemoryType`, and
`ProfileSource` for the cluster of string keys that today live as
free-form `str` arguments.
"""

from __future__ import annotations

from enum import StrEnum


class MemoryEventKind(StrEnum):
    SET = "set"
    DELETE = "delete"
    EXPIRE = "expire"


class MemorySource(StrEnum):
    KV = "kv"
    VECTOR = "vector"


class MemoryType(StrEnum):
    """Vector-memory classification tag.

    The schema in `tools/memory_tools.py` currently advertises
    `{general, fact, episode, summary, preference}`; `DECISION` and
    `TODO` are reserved for upcoming agent surfaces and don't yet
    appear on the wire.
    """

    FACT = "fact"
    EPISODE = "episode"
    PREFERENCE = "preference"
    DECISION = "decision"
    SUMMARY = "summary"
    TODO = "todo"
    GENERAL = "general"


class FilterMode(StrEnum):
    ALL = "all"
    ANY = "any"


class ComparisonOperator(StrEnum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    LT = "lt"
    GTE = "gte"
    LTE = "lte"
    CONTAINS = "contains"


class DBBackendType(StrEnum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"
    QDRANT = "qdrant"


class ProfileSource(StrEnum):
    USER_STATED = "user_stated"
    INFERRED = "inferred"
    OBSERVED = "observed"


__all__ = [
    "ComparisonOperator",
    "DBBackendType",
    "FilterMode",
    "MemoryEventKind",
    "MemorySource",
    "MemoryType",
    "ProfileSource",
]
