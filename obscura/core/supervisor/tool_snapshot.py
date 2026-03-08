"""
obscura.core.supervisor.tool_snapshot — Frozen tool registry per run.

Creates an immutable, ordered snapshot of tools at BUILDING_CONTEXT time.
Prevents tool list flickering by freezing the exact set of tools, their
schemas, and their ordering for the entire run.

Supports both global tool_defs and per-session tool_registrations with
stable ordering (order_index).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from obscura.core.supervisor.schema import init_supervisor_schema
from obscura.core.supervisor.types import SupervisorEvent, SupervisorEventKind

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Frozen tool entry (immutable)
# ---------------------------------------------------------------------------


class FrozenToolEntry:
    """A single tool frozen into a snapshot.

    Immutable after creation. Contains the full schema for replay.
    """

    __slots__ = (
        "name",
        "description",
        "parameters",
        "order_index",
        "tool_id",
        "is_dynamic",
        "schema_hash",
    )

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        order_index: int,
        tool_id: str = "",
        is_dynamic: bool = False,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.order_index = order_index
        self.tool_id = tool_id or name
        self.is_dynamic = is_dynamic
        self.schema_hash = _hash_schema(name, parameters)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "order_index": self.order_index,
            "tool_id": self.tool_id,
            "is_dynamic": self.is_dynamic,
            "schema_hash": self.schema_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FrozenToolEntry:
        return cls(
            name=data["name"],
            description=data["description"],
            parameters=data["parameters"],
            order_index=data["order_index"],
            tool_id=data.get("tool_id", data["name"]),
            is_dynamic=data.get("is_dynamic", False),
        )


# ---------------------------------------------------------------------------
# Frozen tool registry (immutable per run)
# ---------------------------------------------------------------------------


class FrozenToolRegistry:
    """Immutable, ordered tool registry snapshot for a single run.

    Created during BUILDING_CONTEXT. Never modified during the run.
    The AgentLoop receives this instead of the live registry.

    Usage::

        from obscura.core.tools import ToolRegistry

        # From a live registry
        snapshot = FrozenToolRegistry.from_specs(
            specs=[spec1, spec2, spec3],
            allowlist=["tool_a", "tool_b"],
        )

        # Properties
        snapshot.tools      # tuple of FrozenToolEntry (sorted, immutable)
        snapshot.hash        # SHA-256 of all tool schemas
        snapshot.tool_names  # tuple of tool names
    """

    def __init__(
        self,
        tools: tuple[FrozenToolEntry, ...],
        snapshot_id: str = "",
    ) -> None:
        self._tools = tools
        self._snapshot_id = snapshot_id or str(uuid.uuid4())
        self._hash = self._compute_hash()

    @property
    def tools(self) -> tuple[FrozenToolEntry, ...]:
        return self._tools

    @property
    def hash(self) -> str:
        return self._hash

    @property
    def snapshot_id(self) -> str:
        return self._snapshot_id

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(t.name for t in self._tools)

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    def get(self, name: str) -> FrozenToolEntry | None:
        """Look up a tool by name."""
        for tool in self._tools:
            if tool.name == name:
                return tool
        return None

    def contains(self, name: str) -> bool:
        """Check if a tool is in the snapshot."""
        return any(t.name == name for t in self._tools)

    def _compute_hash(self) -> str:
        """Deterministic hash of all tool schemas (sorted by name)."""
        entries = []
        for tool in self._tools:
            entries.append(
                json.dumps(
                    {
                        "name": tool.name,
                        "parameters": tool.parameters,
                    },
                    sort_keys=True,
                )
            )
        combined = "\n".join(entries)
        return hashlib.sha256(combined.encode()).hexdigest()

    def to_json(self) -> str:
        """Serialize for storage."""
        return json.dumps([t.to_dict() for t in self._tools], sort_keys=True)

    @classmethod
    def from_json(cls, data: str, snapshot_id: str = "") -> FrozenToolRegistry:
        """Deserialize from storage."""
        entries = json.loads(data)
        tools = tuple(FrozenToolEntry.from_dict(e) for e in entries)
        return cls(tools=tools, snapshot_id=snapshot_id)

    @classmethod
    def from_specs(
        cls,
        specs: list[Any],
        *,
        allowlist: list[str] | None = None,
        denylist: list[str] | None = None,
    ) -> FrozenToolRegistry:
        """Create a frozen snapshot from ToolSpec objects.

        Tools are sorted alphabetically by name for deterministic ordering.
        Allowlist/denylist filter before freezing.

        Args:
            specs: List of ToolSpec objects (from ToolRegistry.all())
            allowlist: If set, only include these tool names
            denylist: If set, exclude these tool names
        """
        filtered = []
        for spec in specs:
            name = spec.name
            if allowlist and name not in allowlist:
                continue
            if denylist and name in denylist:
                continue
            filtered.append(spec)

        # Sort alphabetically by name for deterministic ordering
        filtered.sort(key=lambda s: s.name)

        tools = tuple(
            FrozenToolEntry(
                name=spec.name,
                description=spec.description,
                parameters=spec.parameters,
                order_index=idx,
                is_dynamic=False,
            )
            for idx, spec in enumerate(filtered)
        )
        return cls(tools=tools)

    @classmethod
    def from_registrations(
        cls,
        registrations: list[dict[str, Any]],
    ) -> FrozenToolRegistry:
        """Create from tool_registrations rows (session-scoped, pre-ordered).

        Registrations already have order_index set. This preserves
        the exact ordering — no re-sorting.
        """
        # Sort by order_index (already set, stable)
        regs = sorted(registrations, key=lambda r: r.get("order_index", 0))

        tools = tuple(
            FrozenToolEntry(
                name=reg["name"],
                description=reg.get("description", ""),
                parameters=reg.get("schema", {}),
                order_index=reg.get("order_index", idx),
                tool_id=reg.get("tool_id", reg["name"]),
                is_dynamic=reg.get("is_dynamic", False),
            )
            for idx, reg in enumerate(regs)
            if reg.get("active", True)
        )
        return cls(tools=tools)

    @classmethod
    def from_broker(
        cls,
        broker: Any,
        allowlist: frozenset[str] | None = None,
    ) -> FrozenToolRegistry:
        """Build a frozen registry from a ToolBroker's registered tools.

        Creates :class:`FrozenToolEntry` objects for each tool known to the
        broker, using schemas stored via ``register_tool()``.

        Args:
            broker: A :class:`~obscura.plugins.broker.ToolBroker` instance.
            allowlist: If provided, only include tools whose names are in this set.
        """
        names: list[str] = broker.registered_tools
        schemas: dict[str, dict[str, Any]] = broker.schemas

        if allowlist is not None:
            names = [n for n in names if n in allowlist]

        # Sort alphabetically for deterministic ordering
        names.sort()

        tools = tuple(
            FrozenToolEntry(
                name=name,
                description="",
                parameters=schemas.get(name, {}),
                order_index=idx,
                is_dynamic=False,
            )
            for idx, name in enumerate(names)
        )
        return cls(tools=tools)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class ToolSnapshotStore:
    """Persists and retrieves tool snapshots."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._local = threading.local()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self) -> None:
        init_supervisor_schema(self._conn())

    def save(self, snapshot: FrozenToolRegistry, run_id: str) -> None:
        """Persist a tool snapshot (sync)."""
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO tool_snapshots "
            "(snapshot_id, run_id, tools_hash, tools_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                snapshot.snapshot_id,
                run_id,
                snapshot.hash,
                snapshot.to_json(),
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()

    def load(self, snapshot_id: str) -> FrozenToolRegistry | None:
        """Load a tool snapshot by ID (sync)."""
        row = self._conn().execute(
            "SELECT snapshot_id, tools_json FROM tool_snapshots "
            "WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        return FrozenToolRegistry.from_json(
            row["tools_json"], snapshot_id=row["snapshot_id"]
        )

    def load_for_run(self, run_id: str) -> FrozenToolRegistry | None:
        """Load the tool snapshot used in a specific run (sync)."""
        row = self._conn().execute(
            "SELECT snapshot_id, tools_json FROM tool_snapshots "
            "WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return FrozenToolRegistry.from_json(
            row["tools_json"], snapshot_id=row["snapshot_id"]
        )

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_schema(name: str, parameters: dict[str, Any]) -> str:
    """Hash a single tool's schema for versioning."""
    data = json.dumps({"name": name, "parameters": parameters}, sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()[:16]
