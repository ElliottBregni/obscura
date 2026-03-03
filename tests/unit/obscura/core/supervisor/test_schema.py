"""Tests for supervisor schema initialization and verification."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from obscura.core.supervisor.schema import (
    REQUIRED_TABLES,
    init_supervisor_schema,
    verify_supervisor_schema,
)


class TestSupervisorSchema:
    def test_init_creates_all_tables(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        init_supervisor_schema(conn)
        missing = verify_supervisor_schema(conn)
        assert missing == [], f"Missing tables: {missing}"
        conn.close()

    def test_init_is_idempotent(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        init_supervisor_schema(conn)
        init_supervisor_schema(conn)  # second time should not raise
        missing = verify_supervisor_schema(conn)
        assert missing == []
        conn.close()

    def test_all_required_tables_defined(self) -> None:
        assert len(REQUIRED_TABLES) == 14
        assert "supervisor_runs" in REQUIRED_TABLES
        assert "supervisor_events" in REQUIRED_TABLES
        assert "session_locks" in REQUIRED_TABLES
        assert "tool_snapshots" in REQUIRED_TABLES
        assert "memory_commits" in REQUIRED_TABLES
        assert "session_hooks" in REQUIRED_TABLES
        assert "session_heartbeats" in REQUIRED_TABLES
        assert "agent_templates" in REQUIRED_TABLES
        assert "agent_versions" in REQUIRED_TABLES
        assert "tool_defs" in REQUIRED_TABLES
        assert "tool_registrations" in REQUIRED_TABLES
        assert "policy_versions" in REQUIRED_TABLES
        assert "memory_items" in REQUIRED_TABLES
        assert "prompt_snapshots" in REQUIRED_TABLES

    def test_verify_detects_missing(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        # Don't init schema
        missing = verify_supervisor_schema(conn)
        assert len(missing) == len(REQUIRED_TABLES)
        conn.close()

    def test_wal_mode_works(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA journal_mode=WAL")
        init_supervisor_schema(conn)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        conn.close()

    def test_foreign_keys_valid(self, tmp_path: Path) -> None:
        """Insert parent before child — FK constraints hold."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA foreign_keys=ON")
        init_supervisor_schema(conn)

        # Create a template first (parent)
        conn.execute(
            "INSERT INTO agent_templates "
            "(template_id, name, template_json, created_at, updated_at) "
            "VALUES ('t1', 'test', '{}', '2024-01-01', '2024-01-01')"
        )

        # Create a version (child)
        conn.execute(
            "INSERT INTO agent_versions "
            "(agent_id, template_id, version, render_json, hash, created_at) "
            "VALUES ('v1', 't1', 1, '{}', 'abc', '2024-01-01')"
        )
        conn.commit()

        # Verify it was inserted
        row = conn.execute("SELECT * FROM agent_versions WHERE agent_id = 'v1'").fetchone()
        assert row is not None
        conn.close()
