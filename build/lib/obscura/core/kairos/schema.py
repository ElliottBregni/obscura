"""obscura.core.kairos.schema — SQLite schema for the Kairos goal runtime.

Single DB file (can share with supervisor or be separate).
Full replay support. Idempotent (CREATE IF NOT EXISTS).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Full schema DDL
# ---------------------------------------------------------------------------

KAIROS_SCHEMA = """
-- =========================================================================
-- 1. GOALS
-- =========================================================================

CREATE TABLE IF NOT EXISTS kairos_goals (
    goal_id          TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    success_criteria TEXT NOT NULL DEFAULT '[]',   -- JSON array of strings
    session_id       TEXT NOT NULL DEFAULT '',
    owner_id         TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'pending',
    budget_json      TEXT NOT NULL DEFAULT '{}',
    tool_allowlist   TEXT NOT NULL DEFAULT '[]',   -- JSON array
    tool_blocklist   TEXT NOT NULL DEFAULT '[]',   -- JSON array
    tags             TEXT NOT NULL DEFAULT '[]',   -- JSON array
    metadata         TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    completed_at     TEXT,
    deadline         TEXT
);

CREATE INDEX IF NOT EXISTS idx_kairos_goals_status
    ON kairos_goals(status);
CREATE INDEX IF NOT EXISTS idx_kairos_goals_session
    ON kairos_goals(session_id);
CREATE INDEX IF NOT EXISTS idx_kairos_goals_owner
    ON kairos_goals(owner_id);

-- =========================================================================
-- 2. PLANS
-- =========================================================================

CREATE TABLE IF NOT EXISTS kairos_plans (
    plan_id      TEXT PRIMARY KEY,
    goal_id      TEXT NOT NULL,
    revision     INTEGER NOT NULL DEFAULT 0,
    rationale    TEXT NOT NULL DEFAULT '',
    task_ids     TEXT NOT NULL DEFAULT '[]',   -- JSON array, ordered
    status       TEXT NOT NULL DEFAULT 'draft',
    metadata     TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (goal_id) REFERENCES kairos_goals(goal_id)
);

CREATE INDEX IF NOT EXISTS idx_kairos_plans_goal
    ON kairos_plans(goal_id, revision DESC);
CREATE INDEX IF NOT EXISTS idx_kairos_plans_status
    ON kairos_plans(status);

-- =========================================================================
-- 3. TASKS
-- =========================================================================

CREATE TABLE IF NOT EXISTS kairos_tasks (
    task_id      TEXT PRIMARY KEY,
    goal_id      TEXT NOT NULL,
    plan_id      TEXT NOT NULL,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    order_index  INTEGER NOT NULL DEFAULT 0,
    depends_on   TEXT NOT NULL DEFAULT '[]',   -- JSON array of task_ids
    tool_hint    TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    max_retries  INTEGER NOT NULL DEFAULT 3,
    retry_count  INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'pending',
    metadata     TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    completed_at TEXT,
    FOREIGN KEY (goal_id)  REFERENCES kairos_goals(goal_id),
    FOREIGN KEY (plan_id)  REFERENCES kairos_plans(plan_id)
);

CREATE INDEX IF NOT EXISTS idx_kairos_tasks_goal
    ON kairos_tasks(goal_id);
CREATE INDEX IF NOT EXISTS idx_kairos_tasks_plan
    ON kairos_tasks(plan_id, order_index);
CREATE INDEX IF NOT EXISTS idx_kairos_tasks_status
    ON kairos_tasks(status);

-- =========================================================================
-- 4. TASK RESULTS
-- =========================================================================

CREATE TABLE IF NOT EXISTS kairos_task_results (
    result_id    TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    goal_id      TEXT NOT NULL,
    plan_id      TEXT NOT NULL,
    status       TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',
    output       TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    turns_used   INTEGER NOT NULL DEFAULT 0,
    tokens_used  INTEGER NOT NULL DEFAULT 0,
    elapsed_ms   INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES kairos_tasks(task_id)
);

CREATE INDEX IF NOT EXISTS idx_kairos_task_results_task
    ON kairos_task_results(task_id);
CREATE INDEX IF NOT EXISTS idx_kairos_task_results_goal
    ON kairos_task_results(goal_id);

-- =========================================================================
-- 5. CHECKPOINTS
-- =========================================================================

CREATE TABLE IF NOT EXISTS kairos_checkpoints (
    checkpoint_id       TEXT PRIMARY KEY,
    goal_id             TEXT NOT NULL,
    plan_id             TEXT NOT NULL,
    kind                TEXT NOT NULL,
    completed_task_ids  TEXT NOT NULL DEFAULT '[]',   -- JSON array
    pending_task_ids    TEXT NOT NULL DEFAULT '[]',   -- JSON array
    summary             TEXT NOT NULL DEFAULT '',
    learnings           TEXT NOT NULL DEFAULT '',
    next_steps          TEXT NOT NULL DEFAULT '',
    budget_usage_json   TEXT NOT NULL DEFAULT '{}',
    metadata            TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    FOREIGN KEY (goal_id)  REFERENCES kairos_goals(goal_id),
    FOREIGN KEY (plan_id)  REFERENCES kairos_plans(plan_id)
);

CREATE INDEX IF NOT EXISTS idx_kairos_checkpoints_goal
    ON kairos_checkpoints(goal_id, created_at DESC);

-- =========================================================================
-- 6. INTERVENTIONS
-- =========================================================================

CREATE TABLE IF NOT EXISTS kairos_interventions (
    intervention_id TEXT PRIMARY KEY,
    goal_id         TEXT NOT NULL,
    task_id         TEXT,
    kind            TEXT NOT NULL,
    question        TEXT NOT NULL,
    context         TEXT NOT NULL DEFAULT '',
    options         TEXT NOT NULL DEFAULT '[]',   -- JSON array of strings
    response        TEXT,
    resolved        INTEGER NOT NULL DEFAULT 0,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    FOREIGN KEY (goal_id) REFERENCES kairos_goals(goal_id)
);

CREATE INDEX IF NOT EXISTS idx_kairos_interventions_goal
    ON kairos_interventions(goal_id);
CREATE INDEX IF NOT EXISTS idx_kairos_interventions_resolved
    ON kairos_interventions(resolved, created_at DESC);

-- =========================================================================
-- 7. EVENT LOG (append-only)
-- =========================================================================

CREATE TABLE IF NOT EXISTS kairos_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id   TEXT NOT NULL,
    plan_id   TEXT NOT NULL DEFAULT '',
    task_id   TEXT NOT NULL DEFAULT '',
    kind      TEXT NOT NULL,
    payload   TEXT NOT NULL DEFAULT '{}',
    timestamp TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kairos_events_goal
    ON kairos_events(goal_id, id);
CREATE INDEX IF NOT EXISTS idx_kairos_events_kind
    ON kairos_events(kind, timestamp);

-- =========================================================================
-- 8. BUDGET TRACKING
-- =========================================================================

CREATE TABLE IF NOT EXISTS kairos_budget_usage (
    goal_id         TEXT PRIMARY KEY,
    tasks_run       INTEGER NOT NULL DEFAULT 0,
    turns_used      INTEGER NOT NULL DEFAULT 0,
    elapsed_seconds REAL NOT NULL DEFAULT 0.0,
    tokens_used     INTEGER NOT NULL DEFAULT 0,
    retries_used    INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (goal_id) REFERENCES kairos_goals(goal_id)
);
"""


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------


REQUIRED_TABLES = (
    "kairos_goals",
    "kairos_plans",
    "kairos_tasks",
    "kairos_task_results",
    "kairos_checkpoints",
    "kairos_interventions",
    "kairos_events",
    "kairos_budget_usage",
)


def init_kairos_schema(conn: "sqlite3.Connection") -> None:
    """Initialize all Kairos tables (idempotent).

    Safe to call multiple times — uses CREATE IF NOT EXISTS.
    """
    conn.executescript(KAIROS_SCHEMA)
    conn.commit()
    logger.debug("Kairos schema initialized (%d tables)", len(REQUIRED_TABLES))


def verify_kairos_schema(conn: "sqlite3.Connection") -> list[str]:
    """Verify all Kairos tables exist. Returns list of missing tables."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'",
    ).fetchall()
    existing = {row[0] for row in rows}
    missing = [t for t in REQUIRED_TABLES if t not in existing]
    if missing:
        logger.warning("Missing Kairos tables: %s", missing)
    return missing
