"""
obscura.core.supervisor.agent_templates — Agent templating + versioning.

Templates are mutable (you can update them).
Versions are immutable (never edited, always create a new one).

A template contains placeholders:
    {{project_name}}, {{tool_bundle}}, {{safety_profile}}, etc.

A version is a fully resolved, rendered agent definition with a
content hash. Runs reference agent_versions, never templates directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from obscura.core.supervisor.schema import init_supervisor_schema

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentTemplate:
    """A reusable, mutable agent template."""

    template_id: str
    name: str
    description: str
    template_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @property
    def system_prompt_template(self) -> str:
        return self.template_json.get("system_prompt", "")

    @property
    def tool_bundles(self) -> list[str]:
        return self.template_json.get("tool_bundles", [])

    @property
    def variables(self) -> list[str]:
        """Extract placeholder variable names from the template."""
        text = json.dumps(self.template_json)
        return sorted(set(re.findall(r"\{\{(\w+)\}\}", text)))


@dataclass(frozen=True)
class AgentVersion:
    """An immutable, rendered agent version."""

    agent_id: str
    template_id: str
    version: int
    render_json: dict[str, Any]
    variables: dict[str, str]
    hash: str
    created_at: datetime

    @property
    def system_prompt(self) -> str:
        return self.render_json.get("system_prompt", "")

    @property
    def tool_names(self) -> list[str]:
        return self.render_json.get("tools", [])


# ---------------------------------------------------------------------------
# Template store
# ---------------------------------------------------------------------------


class AgentTemplateStore:
    """CRUD for agent templates and immutable versions.

    Usage::

        store = AgentTemplateStore("/tmp/supervisor.db")

        # Create a template
        tmpl = store.create_template(
            name="code-architect",
            description="Code architecture agent",
            template_json={
                "system_prompt": "You are a {{role}} for {{project_name}}.",
                "tool_bundles": ["code_tools", "git_tools"],
                "safety_profile": "{{safety_profile}}",
            },
        )

        # Render a version
        ver = store.render_version(
            template_id=tmpl.template_id,
            variables={
                "role": "code architect",
                "project_name": "Obscura",
                "safety_profile": "standard",
            },
        )

        # Use version in a run
        run.agent_id = ver.agent_id
    """

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

    # -- templates -----------------------------------------------------------

    def create_template(
        self,
        name: str,
        *,
        description: str = "",
        template_json: dict[str, Any] | None = None,
    ) -> AgentTemplate:
        """Create a new agent template."""
        template_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        tmpl_json = template_json or {}

        conn = self._conn()
        conn.execute(
            "INSERT INTO agent_templates "
            "(template_id, name, description, template_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                template_id,
                name,
                description,
                json.dumps(tmpl_json, sort_keys=True),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        conn.commit()

        return AgentTemplate(
            template_id=template_id,
            name=name,
            description=description,
            template_json=tmpl_json,
            created_at=now,
            updated_at=now,
        )

    def get_template(self, template_id: str) -> AgentTemplate | None:
        """Get a template by ID."""
        row = self._conn().execute(
            "SELECT * FROM agent_templates WHERE template_id = ?",
            (template_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_template(row)

    def get_template_by_name(self, name: str) -> AgentTemplate | None:
        """Get a template by name."""
        row = self._conn().execute(
            "SELECT * FROM agent_templates WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_template(row)

    def list_templates(self) -> list[AgentTemplate]:
        """List all templates."""
        rows = self._conn().execute(
            "SELECT * FROM agent_templates ORDER BY name"
        ).fetchall()
        return [self._row_to_template(r) for r in rows]

    def update_template(
        self,
        template_id: str,
        *,
        description: str | None = None,
        template_json: dict[str, Any] | None = None,
    ) -> AgentTemplate | None:
        """Update a template (creates new versions, doesn't mutate old ones)."""
        conn = self._conn()
        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [datetime.now(UTC).isoformat()]

        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if template_json is not None:
            sets.append("template_json = ?")
            params.append(json.dumps(template_json, sort_keys=True))

        params.append(template_id)
        conn.execute(
            f"UPDATE agent_templates SET {', '.join(sets)} WHERE template_id = ?",
            params,
        )
        conn.commit()
        return self.get_template(template_id)

    # -- versions ------------------------------------------------------------

    def render_version(
        self,
        template_id: str,
        variables: dict[str, str] | None = None,
    ) -> AgentVersion:
        """Render a template into an immutable version.

        Resolves all {{placeholders}} with provided variables.
        Creates a new version row (never mutated).
        """
        tmpl = self.get_template(template_id)
        if tmpl is None:
            raise ValueError(f"Template not found: {template_id}")

        vars_ = variables or {}
        conn = self._conn()

        # Get next version number
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS max_ver "
            "FROM agent_versions WHERE template_id = ?",
            (template_id,),
        ).fetchone()
        next_version = row["max_ver"] + 1

        # Render: replace {{var}} in template_json
        rendered = _render_template(tmpl.template_json, vars_)
        rendered_str = json.dumps(rendered, sort_keys=True)
        content_hash = hashlib.sha256(rendered_str.encode()).hexdigest()

        agent_id = str(uuid.uuid4())
        now = datetime.now(UTC)

        conn.execute(
            "INSERT INTO agent_versions "
            "(agent_id, template_id, version, render_json, variables, hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                agent_id,
                template_id,
                next_version,
                rendered_str,
                json.dumps(vars_, sort_keys=True),
                content_hash,
                now.isoformat(),
            ),
        )
        conn.commit()

        return AgentVersion(
            agent_id=agent_id,
            template_id=template_id,
            version=next_version,
            render_json=rendered,
            variables=vars_,
            hash=content_hash,
            created_at=now,
        )

    def get_version(self, agent_id: str) -> AgentVersion | None:
        """Get a version by agent_id."""
        row = self._conn().execute(
            "SELECT * FROM agent_versions WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_version(row)

    def get_latest_version(self, template_id: str) -> AgentVersion | None:
        """Get the latest version for a template."""
        row = self._conn().execute(
            "SELECT * FROM agent_versions WHERE template_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (template_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_version(row)

    def list_versions(self, template_id: str) -> list[AgentVersion]:
        """List all versions for a template."""
        rows = self._conn().execute(
            "SELECT * FROM agent_versions WHERE template_id = ? "
            "ORDER BY version DESC",
            (template_id,),
        ).fetchall()
        return [self._row_to_version(r) for r in rows]

    # -- internal ------------------------------------------------------------

    @staticmethod
    def _row_to_template(row: sqlite3.Row) -> AgentTemplate:
        raw_json = row["template_json"]
        tmpl_json: dict[str, Any] = {}
        if raw_json:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                tmpl_json = parsed
        return AgentTemplate(
            template_id=row["template_id"],
            name=row["name"],
            description=row["description"] or "",
            template_json=tmpl_json,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _row_to_version(row: sqlite3.Row) -> AgentVersion:
        render_raw = row["render_json"]
        render_json: dict[str, Any] = {}
        if render_raw:
            parsed = json.loads(render_raw)
            if isinstance(parsed, dict):
                render_json = parsed

        vars_raw = row["variables"]
        variables: dict[str, str] = {}
        if vars_raw:
            parsed_vars = json.loads(vars_raw)
            if isinstance(parsed_vars, dict):
                variables = parsed_vars

        return AgentVersion(
            agent_id=row["agent_id"],
            template_id=row["template_id"],
            version=row["version"],
            render_json=render_json,
            variables=variables,
            hash=row["hash"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def _render_template(
    obj: Any,
    variables: dict[str, str],
) -> Any:
    """Recursively render {{placeholders}} in a JSON-like structure."""
    if isinstance(obj, str):
        for key, value in variables.items():
            obj = obj.replace(f"{{{{{key}}}}}", value)
        return obj
    if isinstance(obj, dict):
        return {k: _render_template(v, variables) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_render_template(item, variables) for item in obj]
    return obj
