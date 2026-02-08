"""
sdk/vector_memory_filters — Metadata filters for vector memory search.

Generates SQL WHERE clauses to pre-filter candidates before vector comparison,
reducing the scan size and improving search performance.

Usage::

    from sdk.vector_memory_filters import DateRangeFilter, TagFilter, FilterBuilder

    filters = [
        DateRangeFilter(field="created_at", start=datetime(2025, 1, 1)),
        TagFilter(tags=["important"], mode="any"),
    ]
    where_clause, params = FilterBuilder.build_sql(filters)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass
class DateRangeFilter:
    """Filter by a timestamp column range."""
    field: str = "created_at"  # "created_at" or "updated_at"
    start: datetime | None = None
    end: datetime | None = None


@dataclass
class TagFilter:
    """Filter by tags stored in metadata JSON."""
    tags: list[str]
    mode: Literal["all", "any"] = "any"


@dataclass
class KeyValueFilter:
    """Filter by a metadata JSON key/value pair."""
    key: str
    value: Any
    operator: Literal["eq", "ne", "gt", "lt", "gte", "lte", "contains"] = "eq"


@dataclass
class MemoryTypeFilter:
    """Filter by memory_type column."""
    memory_types: list[str]


MetadataFilter = DateRangeFilter | TagFilter | KeyValueFilter | MemoryTypeFilter


class FilterBuilder:
    """Build SQL WHERE clause fragments from filter objects."""

    _ALLOWED_DATE_FIELDS = {"created_at", "updated_at"}
    _OP_MAP = {
        "eq": "=",
        "ne": "!=",
        "gt": ">",
        "lt": "<",
        "gte": ">=",
        "lte": "<=",
    }

    @classmethod
    def build_sql(cls, filters: list[MetadataFilter]) -> tuple[str, list[Any]]:
        """
        Convert filters into a SQL WHERE clause and parameter list.

        Returns:
            (clause, params) where clause is like "AND memory_type IN (?,?) AND ..."
            The leading "AND" is included so it can be appended directly.
        """
        clauses: list[str] = []
        params: list[Any] = []

        for f in filters:
            c, p = cls._build_one(f)
            if c:
                clauses.append(c)
                params.extend(p)

        if not clauses:
            return "", []

        return " AND " + " AND ".join(clauses), params

    @classmethod
    def _build_one(cls, f: MetadataFilter) -> tuple[str, list[Any]]:
        if isinstance(f, MemoryTypeFilter):
            if not f.memory_types:
                return "", []
            placeholders = ",".join("?" for _ in f.memory_types)
            return f"memory_type IN ({placeholders})", list(f.memory_types)

        if isinstance(f, DateRangeFilter):
            if f.field not in cls._ALLOWED_DATE_FIELDS:
                raise ValueError(f"DateRangeFilter field must be one of {cls._ALLOWED_DATE_FIELDS}")
            parts: list[str] = []
            params: list[Any] = []
            if f.start is not None:
                parts.append(f"{f.field} >= ?")
                params.append(f.start.isoformat())
            if f.end is not None:
                parts.append(f"{f.field} <= ?")
                params.append(f.end.isoformat())
            if not parts:
                return "", []
            return " AND ".join(parts), params

        if isinstance(f, TagFilter):
            if not f.tags:
                return "", []
            # Uses json_extract on the metadata column to check tags
            # Expects metadata to have a "tags" key with a JSON array
            if f.mode == "any":
                conditions = []
                params = []
                for tag in f.tags:
                    conditions.append("metadata LIKE ?")
                    params.append(f'%"{tag}"%')
                return "(" + " OR ".join(conditions) + ")", params
            else:  # "all"
                conditions = []
                params = []
                for tag in f.tags:
                    conditions.append("metadata LIKE ?")
                    params.append(f'%"{tag}"%')
                return "(" + " AND ".join(conditions) + ")", params

        if isinstance(f, KeyValueFilter):
            json_path = f"$.{f.key}"
            if f.operator == "contains":
                return "json_extract(metadata, ?) LIKE ?", [json_path, f"%{f.value}%"]
            op = cls._OP_MAP.get(f.operator)
            if op is None:
                raise ValueError(f"Unknown operator: {f.operator}")
            return f"json_extract(metadata, ?) {op} ?", [json_path, f.value]

        raise TypeError(f"Unknown filter type: {type(f)}")
