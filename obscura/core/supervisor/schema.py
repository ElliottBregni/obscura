"""
obscura.core.supervisor.schema — Complete SQLite schema for the supervisor.

Single DB file. All tables. Full replay support.
Idempotent (CREATE IF NOT EXISTS + ALTER migrations).
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Full schema DDL
# ---------------------------------------------------------------------------

SUPERVISOR_SCHEMA = """
-- =========================================================================
-- 1. AGENT TEMPLATING
-- =========================================================================

-- Agent templates: reusable, mutable definitions
CREATE TABLE IF NOT EXISTS agent_templates (
    template_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    template_json TEXT NOT NULL,    -- system prompt with placeholders, tool bundles, etc.
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_templates_name
    ON agent_templates(name);

-- Agent versions: immutable rendered instances
CREATE TABLE IF NOT EXISTS agent_versions (
    agent_id     TEXT PRIMARY KEY,
    template_id  TEXT NOT NULL,
    version      INTEGER NOT NULL,
    render_json  TEXT NOT NULL,     -- resolved prompt + settings (no placeholders)
    variables    TEXT DEFAULT '{}', -- variables used to render
    hash         TEXT NOT NULL,     -- SHA-256 of rendered definition
    created_at   TEXT NOT NULL,
    UNIQUE(template_id, version),
    FOREIGN KEY (template_id) REFERENCES agent_templates(template_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_versions_template
    ON agent_versions(template_id, version DESC);

-- =========================================================================
-- 2. TOOL DEFINITIONS + REGISTRATIONS
-- =========================================================================

-- Tool definitions: global catalog (versioned by hash)
CREATE TABLE IF NOT EXISTS tool_defs (
    tool_id      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    schema_json  TEXT NOT NULL,     -- JSON Schema parameters
    binding_json TEXT DEFAULT '{}', -- maps to internal handler
    is_dynamic   INTEGER DEFAULT 0,
    hash         TEXT NOT NULL,     -- SHA-256 of name + schema
    created_at   TEXT NOT NULL,
    retired_at   TEXT              -- NULL if active
);

CREATE INDEX IF NOT EXISTS idx_tool_defs_name ON tool_defs(name);
CREATE INDEX IF NOT EXISTS idx_tool_defs_hash ON tool_defs(hash);

-- Tool registrations: session-scoped stable list with ordering
CREATE TABLE IF NOT EXISTS tool_registrations (
    registration_id TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    tool_id         TEXT NOT NULL,
    alias           TEXT,          -- name exposed to model (NULL = use tool_defs.name)
    order_index     INTEGER NOT NULL,
    active          INTEGER DEFAULT 1,
    created_at      TEXT NOT NULL,
    UNIQUE(session_id, tool_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (tool_id) REFERENCES tool_defs(tool_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_registrations_session
    ON tool_registrations(session_id, order_index);

-- =========================================================================
-- 3. POLICY VERSIONING
-- =========================================================================

-- Policy versions: immutable policy snapshots
CREATE TABLE IF NOT EXISTS policy_versions (
    policy_id   TEXT PRIMARY KEY,
    scope       TEXT NOT NULL DEFAULT 'global',  -- global/agent/session
    scope_id    TEXT NOT NULL DEFAULT '',         -- agent_id or session_id
    version     INTEGER NOT NULL,
    policy_json TEXT NOT NULL,     -- budgets, confirmations, allowlists, etc.
    hash        TEXT NOT NULL,     -- SHA-256 of policy_json
    created_at  TEXT NOT NULL,
    UNIQUE(scope, scope_id, version)
);

CREATE INDEX IF NOT EXISTS idx_policy_versions_scope
    ON policy_versions(scope, scope_id, version DESC);

-- =========================================================================
-- 4. SESSIONS + RUNS + EVENTS
-- =========================================================================

-- Supervisor runs: one row per supervisor.run() invocation
CREATE TABLE IF NOT EXISTS supervisor_runs (
    run_id           TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    agent_id         TEXT,          -- agent_version used
    policy_id        TEXT,          -- policy_version used
    state            TEXT NOT NULL DEFAULT 'idle',
    prompt_hash      TEXT,
    tool_snapshot_id  TEXT,
    tool_registry_hash TEXT,
    memory_query     TEXT,          -- the query used for memory retrieval
    memory_item_ids  TEXT DEFAULT '[]',  -- JSON array of retrieved memory IDs
    memory_snapshot  TEXT DEFAULT '[]',
    turn_count       INTEGER DEFAULT 0,
    started_at       TEXT,
    completed_at     TEXT,
    error            TEXT,
    metadata         TEXT DEFAULT '{}',
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (agent_id) REFERENCES agent_versions(agent_id),
    FOREIGN KEY (policy_id) REFERENCES policy_versions(policy_id)
);

CREATE INDEX IF NOT EXISTS idx_supervisor_runs_session
    ON supervisor_runs(session_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_supervisor_runs_state
    ON supervisor_runs(state);

-- Supervisor event log: append-only, per-run
CREATE TABLE IF NOT EXISTS supervisor_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    payload   TEXT NOT NULL DEFAULT '{}',
    timestamp TEXT NOT NULL,
    UNIQUE(run_id, seq),
    FOREIGN KEY (run_id) REFERENCES supervisor_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_supervisor_events_run
    ON supervisor_events(run_id, seq);

-- =========================================================================
-- 5. MEMORY (canonical truth)
-- =========================================================================

-- Memory items: canonical source-of-truth (Qdrant mirrors this)
CREATE TABLE IF NOT EXISTS memory_items (
    memory_id   TEXT PRIMARY KEY,
    session_id  TEXT,               -- NULL for global memories
    kind        TEXT NOT NULL DEFAULT 'fact',  -- fact/decision/todo/preference
    content     TEXT NOT NULL,
    hash        TEXT NOT NULL,      -- SHA-256 of content (dedupe)
    importance  REAL DEFAULT 0.5,
    pinned      INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    deleted_at  TEXT,               -- soft delete
    UNIQUE(session_id, hash)
);

CREATE INDEX IF NOT EXISTS idx_memory_items_session
    ON memory_items(session_id);
CREATE INDEX IF NOT EXISTS idx_memory_items_hash
    ON memory_items(hash);
CREATE INDEX IF NOT EXISTS idx_memory_items_kind
    ON memory_items(kind);

-- Memory commits: per-run memory writes (for replay auditing)
CREATE TABLE IF NOT EXISTS memory_commits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    key          TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    importance   REAL DEFAULT 0.5,
    pinned       INTEGER DEFAULT 0,
    committed_at TEXT NOT NULL,
    UNIQUE(session_id, content_hash),
    FOREIGN KEY (run_id) REFERENCES supervisor_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_commits_session
    ON memory_commits(session_id);
CREATE INDEX IF NOT EXISTS idx_memory_commits_run
    ON memory_commits(run_id);

-- =========================================================================
-- 6. SESSION LOCKS
-- =========================================================================

-- Advisory locks for single-writer semantics
CREATE TABLE IF NOT EXISTS session_locks (
    session_id   TEXT PRIMARY KEY,
    holder_id    TEXT NOT NULL,
    acquired_at  TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);

-- =========================================================================
-- 7. TOOL SNAPSHOTS (frozen per run)
-- =========================================================================

CREATE TABLE IF NOT EXISTS tool_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    tools_hash  TEXT NOT NULL,
    tools_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES supervisor_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_snapshots_run
    ON tool_snapshots(run_id);

-- =========================================================================
-- 8. SESSION HOOKS (first-class, persisted)
-- =========================================================================

CREATE TABLE IF NOT EXISTS session_hooks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    hook_point  TEXT NOT NULL,
    hook_type   TEXT NOT NULL DEFAULT 'after',
    handler_ref TEXT NOT NULL,
    priority    INTEGER DEFAULT 0,
    active      INTEGER DEFAULT 1,
    created_at  TEXT NOT NULL,
    UNIQUE(session_id, hook_point, handler_ref)
);

CREATE INDEX IF NOT EXISTS idx_session_hooks_session
    ON session_hooks(session_id, hook_point);

-- =========================================================================
-- 9. SESSION HEARTBEATS (first-class, persisted)
-- =========================================================================

CREATE TABLE IF NOT EXISTS session_heartbeats (
    session_id  TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    state       TEXT NOT NULL,
    turn_number INTEGER DEFAULT 0,
    elapsed_ms  INTEGER DEFAULT 0,
    timestamp   TEXT NOT NULL,
    metadata    TEXT DEFAULT '{}',
    PRIMARY KEY (session_id, run_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_session_heartbeats_run
    ON session_heartbeats(run_id);

-- =========================================================================
-- 10. PROMPT SNAPSHOTS (optional full prompt storage)
-- =========================================================================

CREATE TABLE IF NOT EXISTS prompt_snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    prompt_hash  TEXT NOT NULL,
    sections_json TEXT NOT NULL,  -- full prompt sections (optional, configurable)
    token_count  INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES supervisor_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_prompt_snapshots_run
    ON prompt_snapshots(run_id);
"""


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------


def init_supervisor_schema(conn: sqlite3.Connection) -> None:
    """Initialize all supervisor tables (idempotent).

    Safe to call multiple times — uses CREATE IF NOT EXISTS.
    """
    conn.executescript(SUPERVISOR_SCHEMA)
    conn.commit()
    logger.debug("Supervisor schema initialized")


REQUIRED_TABLES = (
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


def verify_supervisor_schema(conn: sqlite3.Connection) -> list[str]:
    """Verify all supervisor tables exist. Returns list of missing tables."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    existing = {row[0] for row in rows}
    missing = [t for t in REQUIRED_TABLES if t not in existing]
    if missing:
        logger.warning("Missing supervisor tables: %s", missing)
    return missing
