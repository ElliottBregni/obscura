"""obscura.core.supervisor.postgres_schema — PostgreSQL DDL for supervisor tables.

Mirrors the 14 tables in ``schema.py`` but uses PostgreSQL-native types:
TIMESTAMPTZ, JSONB, SERIAL.  All tables are placed in the ``supervisor``
schema to keep them isolated from the event-store tables (which live in
the ``events`` schema).

Idempotent (CREATE IF NOT EXISTS).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


SUPERVISOR_SCHEMA_PG = """
-- Create schema
CREATE SCHEMA IF NOT EXISTS supervisor;

-- =========================================================================
-- 1. AGENT TEMPLATING
-- =========================================================================

CREATE TABLE IF NOT EXISTS supervisor.agent_templates (
    template_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    template_json JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_templates_name
    ON supervisor.agent_templates(name);

CREATE TABLE IF NOT EXISTS supervisor.agent_versions (
    agent_id     TEXT PRIMARY KEY,
    template_id  TEXT NOT NULL,
    version      INTEGER NOT NULL,
    render_json  JSONB NOT NULL,
    variables    JSONB DEFAULT '{}'::jsonb,
    hash         TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL,
    UNIQUE(template_id, version),
    FOREIGN KEY (template_id) REFERENCES supervisor.agent_templates(template_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_versions_template
    ON supervisor.agent_versions(template_id, version DESC);

-- =========================================================================
-- 2. TOOL DEFINITIONS + REGISTRATIONS
-- =========================================================================

CREATE TABLE IF NOT EXISTS supervisor.tool_defs (
    tool_id      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    schema_json  JSONB NOT NULL,
    binding_json JSONB DEFAULT '{}'::jsonb,
    is_dynamic   INTEGER DEFAULT 0,
    hash         TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL,
    retired_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tool_defs_name ON supervisor.tool_defs(name);
CREATE INDEX IF NOT EXISTS idx_tool_defs_hash ON supervisor.tool_defs(hash);

CREATE TABLE IF NOT EXISTS supervisor.tool_registrations (
    registration_id TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    tool_id         TEXT NOT NULL,
    alias           TEXT,
    order_index     INTEGER NOT NULL,
    active          INTEGER DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL,
    UNIQUE(session_id, tool_id),
    FOREIGN KEY (tool_id) REFERENCES supervisor.tool_defs(tool_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_registrations_session
    ON supervisor.tool_registrations(session_id, order_index);

-- =========================================================================
-- 3. POLICY VERSIONING
-- =========================================================================

CREATE TABLE IF NOT EXISTS supervisor.policy_versions (
    policy_id   TEXT PRIMARY KEY,
    scope       TEXT NOT NULL DEFAULT 'global',
    scope_id    TEXT NOT NULL DEFAULT '',
    version     INTEGER NOT NULL,
    policy_json JSONB NOT NULL,
    hash        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    UNIQUE(scope, scope_id, version)
);

CREATE INDEX IF NOT EXISTS idx_policy_versions_scope
    ON supervisor.policy_versions(scope, scope_id, version DESC);

-- =========================================================================
-- 4. SESSIONS + RUNS + EVENTS
-- =========================================================================

CREATE TABLE IF NOT EXISTS supervisor.supervisor_runs (
    run_id            TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    agent_id          TEXT,
    policy_id         TEXT,
    state             TEXT NOT NULL DEFAULT 'idle',
    prompt_hash       TEXT,
    tool_snapshot_id   TEXT,
    tool_registry_hash TEXT,
    memory_query      TEXT,
    memory_item_ids   JSONB DEFAULT '[]'::jsonb,
    memory_snapshot   JSONB DEFAULT '[]'::jsonb,
    turn_count        INTEGER DEFAULT 0,
    started_at        TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    error             TEXT,
    metadata          JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_supervisor_runs_session
    ON supervisor.supervisor_runs(session_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_supervisor_runs_state
    ON supervisor.supervisor_runs(state);

CREATE TABLE IF NOT EXISTS supervisor.supervisor_events (
    id        SERIAL PRIMARY KEY,
    run_id    TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    payload   JSONB NOT NULL DEFAULT '{}'::jsonb,
    timestamp TIMESTAMPTZ NOT NULL,
    UNIQUE(run_id, seq),
    FOREIGN KEY (run_id) REFERENCES supervisor.supervisor_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_supervisor_events_run
    ON supervisor.supervisor_events(run_id, seq);

-- =========================================================================
-- 5. MEMORY (canonical truth)
-- =========================================================================

CREATE TABLE IF NOT EXISTS supervisor.memory_items (
    memory_id   TEXT PRIMARY KEY,
    session_id  TEXT,
    kind        TEXT NOT NULL DEFAULT 'fact',
    content     TEXT NOT NULL,
    hash        TEXT NOT NULL,
    importance  DOUBLE PRECISION DEFAULT 0.5,
    pinned      INTEGER DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL,
    deleted_at  TIMESTAMPTZ,
    UNIQUE(session_id, hash)
);

CREATE INDEX IF NOT EXISTS idx_memory_items_session
    ON supervisor.memory_items(session_id);
CREATE INDEX IF NOT EXISTS idx_memory_items_hash
    ON supervisor.memory_items(hash);
CREATE INDEX IF NOT EXISTS idx_memory_items_kind
    ON supervisor.memory_items(kind);

CREATE TABLE IF NOT EXISTS supervisor.memory_commits (
    id           SERIAL PRIMARY KEY,
    session_id   TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    key          TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    importance   DOUBLE PRECISION DEFAULT 0.5,
    pinned       INTEGER DEFAULT 0,
    committed_at TIMESTAMPTZ NOT NULL,
    UNIQUE(session_id, content_hash),
    FOREIGN KEY (run_id) REFERENCES supervisor.supervisor_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_commits_session
    ON supervisor.memory_commits(session_id);
CREATE INDEX IF NOT EXISTS idx_memory_commits_run
    ON supervisor.memory_commits(run_id);

-- =========================================================================
-- 6. SESSION LOCKS
-- =========================================================================

CREATE TABLE IF NOT EXISTS supervisor.session_locks (
    session_id   TEXT PRIMARY KEY,
    holder_id    TEXT NOT NULL,
    acquired_at  TIMESTAMPTZ NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL
);

-- =========================================================================
-- 7. TOOL SNAPSHOTS (frozen per run)
-- =========================================================================

CREATE TABLE IF NOT EXISTS supervisor.tool_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    tools_hash  TEXT NOT NULL,
    tools_json  JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (run_id) REFERENCES supervisor.supervisor_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_snapshots_run
    ON supervisor.tool_snapshots(run_id);

-- =========================================================================
-- 8. SESSION HOOKS (first-class, persisted)
-- =========================================================================

CREATE TABLE IF NOT EXISTS supervisor.session_hooks (
    id          SERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL,
    hook_point  TEXT NOT NULL,
    hook_type   TEXT NOT NULL DEFAULT 'after',
    handler_ref TEXT NOT NULL,
    priority    INTEGER DEFAULT 0,
    active      INTEGER DEFAULT 1,
    created_at  TIMESTAMPTZ NOT NULL,
    UNIQUE(session_id, hook_point, handler_ref)
);

CREATE INDEX IF NOT EXISTS idx_session_hooks_session
    ON supervisor.session_hooks(session_id, hook_point);

-- =========================================================================
-- 9. SESSION HEARTBEATS (first-class, persisted)
-- =========================================================================

CREATE TABLE IF NOT EXISTS supervisor.session_heartbeats (
    session_id  TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    state       TEXT NOT NULL,
    turn_number INTEGER DEFAULT 0,
    elapsed_ms  INTEGER DEFAULT 0,
    timestamp   TIMESTAMPTZ NOT NULL,
    metadata    JSONB DEFAULT '{}'::jsonb,
    PRIMARY KEY (session_id, run_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_session_heartbeats_run
    ON supervisor.session_heartbeats(run_id);

-- =========================================================================
-- 10. PROMPT SNAPSHOTS (optional full prompt storage)
-- =========================================================================

CREATE TABLE IF NOT EXISTS supervisor.prompt_snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    prompt_hash  TEXT NOT NULL,
    sections_json JSONB NOT NULL,
    token_count  INTEGER DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (run_id) REFERENCES supervisor.supervisor_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_prompt_snapshots_run
    ON supervisor.prompt_snapshots(run_id);
"""


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------


def init_supervisor_schema_pg(conn: Any) -> None:
    """Initialize all supervisor tables on PostgreSQL (idempotent).

    Expects a psycopg2 connection (not autocommit).
    """
    with conn.cursor() as cur:
        cur.execute(SUPERVISOR_SCHEMA_PG)
    conn.commit()
    logger.debug("PostgreSQL supervisor schema initialized")


REQUIRED_TABLES_PG = (
    "agent_templates",
    "agent_versions",
    "tool_defs",
    "tool_registrations",
    "policy_versions",
    "supervisor_runs",
    "supervisor_events",
    "memory_items",
    "memory_commits",
    "session_locks",
    "tool_snapshots",
    "session_hooks",
    "session_heartbeats",
    "prompt_snapshots",
)


def verify_supervisor_schema_pg(conn: Any) -> list[str]:
    """Verify all supervisor tables exist in PostgreSQL.

    Returns a list of missing table names.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'supervisor'"
        )
        existing = {
            row["table_name"] if hasattr(row, "__getitem__") else row[0]
            for row in cur.fetchall()
        }

    missing = [t for t in REQUIRED_TABLES_PG if t not in existing]
    if missing:
        logger.warning("Missing PostgreSQL supervisor tables: %s", missing)
    return missing
