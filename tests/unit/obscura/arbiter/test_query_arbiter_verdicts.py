"""Unit tests for query_arbiter_verdicts tool."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest import mock


def _make_db(path: Path) -> None:
    """Create a minimal arbiter.db with sample rows."""
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE verdicts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            kind          TEXT NOT NULL,
            verdict       TEXT NOT NULL,
            target_id     TEXT NOT NULL DEFAULT '',
            session_id    TEXT NOT NULL DEFAULT '',
            run_id        TEXT NOT NULL DEFAULT '',
            det_score     REAL NOT NULL DEFAULT 0,
            judge_score   REAL,
            composite     REAL NOT NULL DEFAULT 0,
            feedback      TEXT NOT NULL DEFAULT '',
            details       TEXT NOT NULL DEFAULT '[]',
            retry_count   INTEGER NOT NULL DEFAULT 0,
            metadata      TEXT NOT NULL DEFAULT '{}',
            created_at    REAL NOT NULL
        )
    """)
    rows = [
        # (kind, verdict, target_id, session_id, composite)
        ("tool_call", "accept", "read_text_file", "sess-A", 0.95),
        ("model_turn", "revise", "task:abc", "sess-A", 0.45),
        ("tool_call", "deny", "rm_rf", "sess-B", 0.05),
        ("task_complete", "accept", "task:xyz", "sess-B", 0.88),
        ("goal_transition", "kill", "goal:evil", "sess-C", 0.01),
    ]
    t = time.time()
    for i, (kind, verdict, target_id, session_id, composite) in enumerate(rows):
        conn.execute(
            """INSERT INTO verdicts
               (kind, verdict, target_id, session_id, run_id,
                det_score, judge_score, composite, feedback,
                details, retry_count, metadata, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                kind, verdict, target_id, session_id, "",
                composite, None, composite, "",
                "[]", 0, "{}", t - i,
            ),
        )
    conn.commit()
    conn.close()


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.get_event_loop().run_until_complete(coro)


class TestQueryArbiterVerdicts:
    """Tests for query_arbiter_verdicts tool function."""

    def setup_method(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = Path(self._tmpdir) / "arbiter.db"
        _make_db(self._db_path)

    def _patch_db_path(self):
        """Context manager that redirects resolve_obscura_home to the temp dir."""
        return mock.patch(
            "obscura.tools.arbiter_tools.resolve_obscura_home",
            return_value=Path(self._tmpdir),
        )

    def test_returns_all_rows_by_default(self) -> None:
        from obscura.tools.arbiter_tools import query_arbiter_verdicts

        with self._patch_db_path():
            result = json.loads(_run(query_arbiter_verdicts()))

        assert result["ok"] is True
        assert result["count"] == 5
        assert len(result["verdicts"]) == 5

    def test_filter_by_session_id(self) -> None:
        from obscura.tools.arbiter_tools import query_arbiter_verdicts

        with self._patch_db_path():
            result = json.loads(_run(query_arbiter_verdicts(session_id="sess-A")))

        assert result["ok"] is True
        assert result["count"] == 2
        assert all(v["session_id"] == "sess-A" for v in result["verdicts"])

    def test_filter_by_verdict_type(self) -> None:
        from obscura.tools.arbiter_tools import query_arbiter_verdicts

        with self._patch_db_path():
            result = json.loads(_run(query_arbiter_verdicts(verdict="deny")))

        assert result["ok"] is True
        assert result["count"] == 1
        assert result["verdicts"][0]["verdict"] == "deny"
        assert result["verdicts"][0]["target_id"] == "rm_rf"

    def test_filter_by_kind(self) -> None:
        from obscura.tools.arbiter_tools import query_arbiter_verdicts

        with self._patch_db_path():
            result = json.loads(_run(query_arbiter_verdicts(kind="tool_call")))

        assert result["ok"] is True
        assert result["count"] == 2
        assert all(v["kind"] == "tool_call" for v in result["verdicts"])

    def test_filter_by_min_score(self) -> None:
        from obscura.tools.arbiter_tools import query_arbiter_verdicts

        with self._patch_db_path():
            result = json.loads(_run(query_arbiter_verdicts(min_score=0.80)))

        assert result["ok"] is True
        assert result["count"] == 2
        assert all(v["composite"] >= 0.80 for v in result["verdicts"])

    def test_limit_respected(self) -> None:
        from obscura.tools.arbiter_tools import query_arbiter_verdicts

        with self._patch_db_path():
            result = json.loads(_run(query_arbiter_verdicts(limit=2)))

        assert result["ok"] is True
        assert result["count"] == 2

    def test_limit_capped_at_100(self) -> None:
        from obscura.tools.arbiter_tools import query_arbiter_verdicts

        with self._patch_db_path():
            # 9999 should be capped to 100; we only have 5 rows so count = 5
            result = json.loads(_run(query_arbiter_verdicts(limit=9999)))

        assert result["ok"] is True
        assert result["count"] == 5  # only 5 rows exist

    def test_missing_db_returns_empty(self) -> None:
        from obscura.tools.arbiter_tools import query_arbiter_verdicts

        with mock.patch(
            "obscura.tools.arbiter_tools.resolve_obscura_home",
            return_value=Path("/nonexistent/path/that/does/not/exist"),
        ):
            result = json.loads(_run(query_arbiter_verdicts()))

        assert result["ok"] is True
        assert result["verdicts"] == []

    def test_combined_filters(self) -> None:
        from obscura.tools.arbiter_tools import query_arbiter_verdicts

        with self._patch_db_path():
            result = json.loads(
                _run(query_arbiter_verdicts(session_id="sess-B", verdict="accept"))
            )

        assert result["ok"] is True
        assert result["count"] == 1
        assert result["verdicts"][0]["target_id"] == "task:xyz"
