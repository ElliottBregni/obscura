"""obscura.core.supervisor.policy_store — Immutable policy versioning.

Policies define budgets, confirmations, allowlists, tool restrictions, etc.
Once a version is created, it is never mutated. Runs reference a specific
policy_id for full replay.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from obscura.core.supervisor.db_backend import (
    DatabaseBackend,
    SQLiteSupervisorBackend,
    translate_sql,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyVersion:
    """An immutable policy snapshot."""

    policy_id: str
    scope: str  # "global", "agent", "session"
    scope_id: str  # "" for global, agent_id, or session_id
    version: int
    policy_json: dict[str, Any]
    hash: str
    created_at: datetime

    @property
    def tool_allowlist(self) -> list[str] | None:
        """Explicit tool allowlist (None = all allowed)."""
        return self.policy_json.get("tool_allowlist")

    @property
    def tool_denylist(self) -> list[str]:
        return self.policy_json.get("tool_denylist", [])

    @property
    def require_confirmation(self) -> list[str]:
        """Tool names that require user confirmation."""
        return self.policy_json.get("require_confirmation", [])

    @property
    def max_turns(self) -> int:
        return self.policy_json.get("max_turns", 10)

    @property
    def token_budget(self) -> int:
        return self.policy_json.get("token_budget", 0)

    @property
    def allow_dynamic_tools(self) -> bool:
        return self.policy_json.get("allow_dynamic_tools", False)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class PolicyStore:
    """CRUD for immutable policy versions.

    Usage::

        store = PolicyStore("/tmp/supervisor.db")

        # Create a global policy
        policy = store.create_version(
            scope="global",
            policy_json={
                "tool_allowlist": None,
                "tool_denylist": ["dangerous_tool"],
                "require_confirmation": ["bash", "delete_file"],
                "max_turns": 15,
                "token_budget": 100000,
                "allow_dynamic_tools": True,
            },
        )

        # Reference in a run
        run.policy_id = policy.policy_id
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        backend: DatabaseBackend | None = None,
    ) -> None:
        if backend is not None:
            self._backend = backend
        elif db_path is not None:
            self._backend = SQLiteSupervisorBackend(db_path)
        else:
            msg = "Either db_path or backend must be provided"
            raise ValueError(msg)

    def _sql(self, sql: str) -> str:
        """Translate SQL for the current dialect."""
        return translate_sql(sql, self._backend.dialect)

    def create_version(
        self,
        *,
        scope: str = "global",
        scope_id: str = "",
        policy_json: dict[str, Any] | None = None,
    ) -> PolicyVersion:
        """Create a new immutable policy version."""
        conn = self._backend.get_conn()
        try:
            pjson = policy_json or {}
            pjson_str = json.dumps(pjson, sort_keys=True)
            content_hash = hashlib.sha256(pjson_str.encode()).hexdigest()

            # Get next version number
            cur = conn.execute(
                self._sql(
                    "SELECT COALESCE(MAX(version), 0) AS max_ver "
                    "FROM policy_versions WHERE scope = ? AND scope_id = ?"
                ),
                (scope, scope_id),
            )
            row = cur.fetchone()
            next_version = row["max_ver"] + 1

            policy_id = str(uuid.uuid4())
            now = datetime.now(UTC)

            conn.execute(
                self._sql(
                    "INSERT INTO policy_versions "
                    "(policy_id, scope, scope_id, version, policy_json, hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    policy_id,
                    scope,
                    scope_id,
                    next_version,
                    pjson_str,
                    content_hash,
                    now.isoformat(),
                ),
            )
            conn.commit()
        finally:
            self._backend.put_conn(conn)

        return PolicyVersion(
            policy_id=policy_id,
            scope=scope,
            scope_id=scope_id,
            version=next_version,
            policy_json=pjson,
            hash=content_hash,
            created_at=now,
        )

    def get_version(self, policy_id: str) -> PolicyVersion | None:
        """Get a policy version by ID."""
        conn = self._backend.get_conn()
        try:
            cur = conn.execute(
                self._sql("SELECT * FROM policy_versions WHERE policy_id = ?"),
                (policy_id,),
            )
            row = cur.fetchone()
        finally:
            self._backend.put_conn(conn)
        if row is None:
            return None
        return self._row_to_version(row)

    def get_latest(
        self,
        scope: str = "global",
        scope_id: str = "",
    ) -> PolicyVersion | None:
        """Get the latest policy version for a scope."""
        conn = self._backend.get_conn()
        try:
            cur = conn.execute(
                self._sql(
                    "SELECT * FROM policy_versions "
                    "WHERE scope = ? AND scope_id = ? "
                    "ORDER BY version DESC LIMIT 1"
                ),
                (scope, scope_id),
            )
            row = cur.fetchone()
        finally:
            self._backend.put_conn(conn)
        if row is None:
            return None
        return self._row_to_version(row)

    def list_versions(
        self,
        scope: str = "global",
        scope_id: str = "",
    ) -> list[PolicyVersion]:
        """List all policy versions for a scope."""
        conn = self._backend.get_conn()
        try:
            cur = conn.execute(
                self._sql(
                    "SELECT * FROM policy_versions "
                    "WHERE scope = ? AND scope_id = ? "
                    "ORDER BY version DESC"
                ),
                (scope, scope_id),
            )
            rows = cur.fetchall()
        finally:
            self._backend.put_conn(conn)
        return [self._row_to_version(r) for r in rows]

    # -- internal ------------------------------------------------------------

    @staticmethod
    def _row_to_version(row: Any) -> PolicyVersion:
        raw = row["policy_json"]
        pjson: dict[str, Any] = {}
        if raw:
            parsed: Any = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
                pjson = cast(dict[str, Any], parsed)
        created = row["created_at"]
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        return PolicyVersion(
            policy_id=row["policy_id"],
            scope=row["scope"],
            scope_id=row["scope_id"],
            version=row["version"],
            policy_json=pjson,
            hash=row["hash"],
            created_at=created,
        )

    def close(self) -> None:
        self._backend.close()
