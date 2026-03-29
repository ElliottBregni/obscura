"""
Tests for obscura.tools.system.intelligence — context_snapshot, causal_trace, policy_probe.

Strategy:
- All three tools are async coroutines that return JSON strings.
- context_snapshot / causal_trace: when no supervisor DB exists they return a
  well-structured ``{"status": "no_db", ...}`` response. We exercise that path
  plus a live-DB path using a real SQLite fixture that mimics the supervisor
  schema tables.
- policy_probe: does not require a DB; the ``policy_override`` parameter lets
  us inject a policy inline and exercise the full evaluation path without any
  filesystem or DB dependency.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from obscura.tools.system.intelligence import (
    _open_db,
    _rows,
    causal_trace,
    context_snapshot,
    policy_probe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro: Any) -> Any:  # noqa: ANN401
    """Run a coroutine synchronously (compatible with older pytest-asyncio configs)."""
    return asyncio.get_event_loop().run_until_complete(coro)


def parse(result: str) -> dict[str, Any]:
    return json.loads(result)


# ---------------------------------------------------------------------------
# Minimal supervisor schema fixture
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS supervisor_runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    state TEXT,
    started_at TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS supervisor_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT,
    timestamp TEXT
);
CREATE TABLE IF NOT EXISTS tool_registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_hash TEXT,
    registered_at TEXT
);
CREATE TABLE IF NOT EXISTS memory_items (
    key TEXT PRIMARY KEY,
    content TEXT,
    importance REAL DEFAULT 0.5,
    recency REAL DEFAULT 0.5,
    relevance REAL DEFAULT 0.5,
    pinned INTEGER DEFAULT 0,
    source_run_id TEXT
);
CREATE TABLE IF NOT EXISTS memory_commits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    committed INTEGER DEFAULT 0,
    deduplicated INTEGER DEFAULT 0,
    gated INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    committed_at TEXT
);
CREATE TABLE IF NOT EXISTS prompt_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    prompt_hash TEXT,
    sections_json TEXT,
    assembled_at TEXT
);
CREATE TABLE IF NOT EXISTS policy_versions (
    version_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    policy_json TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS session_heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq INTEGER,
    state TEXT,
    turn_number INTEGER,
    elapsed_ms INTEGER,
    timestamp TEXT
);
CREATE TABLE IF NOT EXISTS session_hooks (
    hook_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    hook_point TEXT,
    handler_name TEXT,
    enabled INTEGER DEFAULT 1,
    registered_at TEXT
);
"""


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Create a minimal supervisor.db with one session + run + events."""
    db_path = tmp_path / "supervisor.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)

    # Insert a run
    conn.execute(
        "INSERT INTO supervisor_runs VALUES (?,?,?,?,?)",
        ("run-1", "sess-1", "COMPLETED", "2024-01-01T00:00:00", "2024-01-01T00:01:00"),
    )

    # Insert some events
    events = [
        ("run-1", "run_started", '{"prompt": "hello"}', "2024-01-01T00:00:01"),
        ("run-1", "state_transition", '{"from": "IDLE", "to": "RUNNING_MODEL"}', "2024-01-01T00:00:02"),
        ("run-1", "model_turn_start", "{}", "2024-01-01T00:00:03"),
        ("run-1", "tool_execution_start", '{"tool": "read_file"}', "2024-01-01T00:00:04"),
        ("run-1", "tool_execution_end", '{"tool": "read_file", "success": true}', "2024-01-01T00:00:05"),
        ("run-1", "memory_commit", '{"committed": 1}', "2024-01-01T00:00:06"),
        ("run-1", "run_completed", "{}", "2024-01-01T00:00:07"),
    ]
    conn.executemany(
        "INSERT INTO supervisor_events (run_id, kind, payload_json, timestamp) VALUES (?,?,?,?)",
        events,
    )

    # Insert a tool registration
    conn.execute(
        "INSERT INTO tool_registrations (run_id, tool_name, tool_hash, registered_at) VALUES (?,?,?,?)",
        ("run-1", "read_file", "abc123", "2024-01-01T00:00:00"),
    )

    # Insert memory items
    conn.execute(
        "INSERT INTO memory_items VALUES (?,?,?,?,?,?,?)",
        ("key1", "some memory content", 0.9, 0.8, 0.7, 0, "run-1"),
    )

    # Insert a policy version
    policy_data = json.dumps({"allow_list": ["read_file", "search_files"], "deny_list": [], "full_access": False})
    conn.execute(
        "INSERT INTO policy_versions VALUES (?,?,?,?)",
        ("pv-1", "sess-1", policy_data, "2024-01-01T00:00:00"),
    )

    # Insert heartbeats
    conn.execute(
        "INSERT INTO session_heartbeats (session_id, seq, state, turn_number, elapsed_ms, timestamp) VALUES (?,?,?,?,?,?)",
        ("sess-1", 1, "RUNNING_MODEL", 1, 500, "2024-01-01T00:00:03"),
    )

    # Insert hooks
    conn.execute(
        "INSERT INTO session_hooks VALUES (?,?,?,?,?,?)",
        ("hook-1", "sess-1", "pre_tool", "my_hook", 1, "2024-01-01T00:00:00"),
    )

    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# _open_db / _rows helpers
# ---------------------------------------------------------------------------


class TestOpenDb:
    def test_returns_none_for_nonexistent_path(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path / "nonexistent.db")
        assert conn is None

    def test_returns_connection_for_existing_db(self, tmp_db: Path) -> None:
        conn = _open_db(tmp_db)
        assert conn is not None
        conn.close()

    def test_row_factory_set(self, tmp_db: Path) -> None:
        conn = _open_db(tmp_db)
        assert conn is not None
        cur = conn.execute("SELECT 1 AS val")
        row = cur.fetchone()
        # row_factory = sqlite3.Row means we can access by column name
        assert row["val"] == 1
        conn.close()


class TestRows:
    def test_returns_list_of_dicts(self, tmp_db: Path) -> None:
        conn = _open_db(tmp_db)
        assert conn is not None
        rows = _rows(conn, "SELECT run_id, session_id FROM supervisor_runs")
        assert len(rows) == 1
        assert rows[0]["run_id"] == "run-1"
        assert rows[0]["session_id"] == "sess-1"
        conn.close()

    def test_returns_empty_list_for_missing_table(self, tmp_db: Path) -> None:
        conn = _open_db(tmp_db)
        assert conn is not None
        rows = _rows(conn, "SELECT * FROM nonexistent_table_xyz")
        assert rows == []
        conn.close()

    def test_parametrized_query(self, tmp_db: Path) -> None:
        conn = _open_db(tmp_db)
        assert conn is not None
        rows = _rows(conn, "SELECT * FROM supervisor_runs WHERE session_id = ?", ("sess-1",))
        assert len(rows) == 1
        rows_none = _rows(conn, "SELECT * FROM supervisor_runs WHERE session_id = ?", ("no-such",))
        assert rows_none == []
        conn.close()


# ---------------------------------------------------------------------------
# context_snapshot
# ---------------------------------------------------------------------------


class TestContextSnapshotNoDb:
    """Behaviour when no supervisor DB exists."""

    def test_returns_no_db_status(self, tmp_path: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_path / "supervisor.db",
        ):
            result = parse(run(context_snapshot()))
        assert result["status"] == "no_db"
        assert "message" in result

    def test_no_db_message_mentions_path(self, tmp_path: Path) -> None:
        db_path = tmp_path / "supervisor.db"
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=db_path,
        ):
            result = parse(run(context_snapshot()))
        assert str(db_path) in result["message"]


class TestContextSnapshotWithDb:
    """Behaviour with a real (minimal) supervisor DB."""

    def test_status_ok(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(context_snapshot()))
        assert result["status"] == "ok"

    def test_resolves_session_and_run(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(context_snapshot()))
        assert result["session_id"] == "sess-1"
        assert result["run_id"] == "run-1"

    def test_includes_all_sections_by_default(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(context_snapshot()))
        assert "run" in result
        assert "tools" in result
        assert "memory" in result
        assert "prompt" in result
        assert "policy" in result
        assert "heartbeats" in result
        assert "hooks" in result

    def test_run_section_has_data(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(context_snapshot()))
        assert result["run"]["run_id"] == "run-1"
        assert result["run"]["session_id"] == "sess-1"

    def test_tools_section_has_registrations(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(context_snapshot()))
        assert len(result["tools"]) == 1
        assert result["tools"][0]["tool_name"] == "read_file"

    def test_memory_section_present(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(context_snapshot()))
        assert len(result["memory"]) == 1
        assert result["memory"][0]["key"] == "key1"

    def test_policy_section_present(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(context_snapshot()))
        assert "policy" in result["policy"]
        assert result["policy"]["policy"]["allow_list"] == ["read_file", "search_files"]

    def test_heartbeats_section(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(context_snapshot()))
        assert len(result["heartbeats"]) == 1
        assert result["heartbeats"][0]["state"] == "RUNNING_MODEL"

    def test_hooks_section(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(context_snapshot()))
        assert len(result["hooks"]) == 1
        assert result["hooks"][0]["hook_point"] == "pre_tool"

    def test_include_filter_run_only(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(context_snapshot(include=["run"])))
        assert "run" in result
        assert "tools" not in result
        assert "memory" not in result
        assert "heartbeats" not in result

    def test_include_filter_tools_and_memory(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(context_snapshot(include=["tools", "memory"])))
        assert "tools" in result
        assert "memory" in result
        assert "run" not in result
        assert "policy" not in result

    def test_explicit_session_id(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(context_snapshot(session_id="sess-1")))
        assert result["status"] == "ok"
        assert result["session_id"] == "sess-1"

    def test_explicit_run_id(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(context_snapshot(run_id="run-1")))
        assert result["status"] == "ok"
        assert result["run_id"] == "run-1"

    def test_unknown_session_returns_empty_sections(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(context_snapshot(session_id="no-such-session", run_id="no-such-run")))
        assert result["status"] == "ok"
        assert result["run"] == {}
        assert result["tools"] == []

    def test_db_path_in_response(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(context_snapshot()))
        assert "db_path" in result
        assert str(tmp_db) == result["db_path"]

    def test_result_is_valid_json(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            raw = run(context_snapshot())
        # Should not raise
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# causal_trace
# ---------------------------------------------------------------------------


class TestCausalTraceNoDb:
    def test_returns_no_db_status(self, tmp_path: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_path / "supervisor.db",
        ):
            result = parse(run(causal_trace()))
        assert result["status"] == "no_db"


class TestCausalTraceWithDb:
    def test_status_ok(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace()))
        assert result["status"] == "ok"

    def test_resolves_session_and_run(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace()))
        assert result["session_id"] == "sess-1"
        assert result["run_id"] == "run-1"

    def test_trace_is_list(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace()))
        assert isinstance(result["trace"], list)
        assert len(result["trace"]) > 0

    def test_last_event_is_terminal(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace()))
        assert result["trace"][-1]["is_terminal"] is True

    def test_event_structure(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace()))
        for event in result["trace"]:
            assert "seq" in event
            assert "kind" in event
            assert "timestamp" in event
            assert "is_terminal" in event
            assert "is_fork_point" in event

    def test_depth_respected(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace(depth=3)))
        assert result["chain_length"] <= 3

    def test_depth_clamped_to_100(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace(depth=9999)))
        # Should not raise and chain_length should be <= 100
        assert result["chain_length"] <= 100

    def test_depth_minimum_1(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace(depth=0)))
        assert result["chain_length"] >= 1

    def test_outcome_filter_run_completed(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace(outcome="run_completed")))
        assert result["status"] == "ok"
        assert result["terminal_event"] == "run_completed"

    def test_outcome_filter_nonexistent_falls_back_to_last(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace(outcome="this_will_never_match_xyz")))
        # Falls back to last event in run
        assert result["status"] == "ok"
        assert len(result["trace"]) > 0

    def test_include_payloads_false_by_default(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace()))
        for event in result["trace"]:
            assert "payload" not in event

    def test_include_payloads_true(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace(include_payloads=True)))
        for event in result["trace"]:
            assert "payload" in event

    def test_event_kind_counts_present(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace()))
        assert isinstance(result["event_kind_counts"], dict)
        assert sum(result["event_kind_counts"].values()) == result["chain_length"]

    def test_total_events_in_run(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace()))
        # We inserted 7 events, all in _CAUSAL_EVENT_KINDS
        assert result["total_events_in_run"] == 7

    def test_no_fork_point_for_clean_run(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace()))
        # No drift_detected or run_failed in our fixture
        assert result["fork_point"] is None

    def test_fork_point_detected_for_failed_run(self, tmp_db: Path) -> None:
        """Insert a drift_detected event and verify fork_point is set."""
        conn = sqlite3.connect(str(tmp_db))
        conn.execute(
            "INSERT INTO supervisor_events (run_id, kind, payload_json, timestamp) "
            "VALUES (?,?,?,?)",
            ("run-1", "drift_detected", '{"reason": "hash mismatch"}', "2024-01-01T00:00:08"),
        )
        conn.commit()
        conn.close()

        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace()))
        assert result["fork_point"] is not None
        assert result["fork_point"]["kind"] == "drift_detected"
        assert result["fork_point"]["is_fork_point"] is True

    def test_explicit_session_and_run(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace(session_id="sess-1", run_id="run-1")))
        assert result["status"] == "ok"

    def test_unknown_run_returns_no_events(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(causal_trace(session_id="no-session", run_id="no-run")))
        # no_events because the run_id doesn't resolve
        assert result["status"] in ("no_events", "ok")

    def test_result_is_valid_json(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            raw = run(causal_trace())
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# policy_probe
# ---------------------------------------------------------------------------


class TestPolicyProbeFullAccess:
    """policy_override with full_access=True — always allows."""

    def test_allows_any_tool(self) -> None:
        result = parse(run(policy_probe(
            tool_name="delete_file",
            policy_override={"full_access": True},
        )))
        assert result["status"] == "ok"
        assert result["allowed"] is True

    def test_full_access_explanation(self) -> None:
        result = parse(run(policy_probe(
            tool_name="shell_exec",
            policy_override={"full_access": True},
            explain=True,
        )))
        assert "full_access" in result["explanation"].lower()

    def test_policy_source_inline(self) -> None:
        result = parse(run(policy_probe(
            tool_name="read_file",
            policy_override={"full_access": True},
        )))
        assert result["policy_source"] == "inline_override"

    def test_policy_summary_full_access_true(self) -> None:
        result = parse(run(policy_probe(
            tool_name="search_files",
            policy_override={"full_access": True},
        )))
        assert result["policy_summary"]["full_access"] is True


class TestPolicyProbeDenyList:
    """policy_override with deny_list."""

    def test_denies_listed_tool(self) -> None:
        result = parse(run(policy_probe(
            tool_name="write_file",
            policy_override={"deny_list": ["write_file", "delete_file"]},
        )))
        assert result["status"] == "ok"
        assert result["allowed"] is False

    def test_allows_unlisted_tool(self) -> None:
        result = parse(run(policy_probe(
            tool_name="read_file",
            policy_override={"deny_list": ["write_file"]},
        )))
        assert result["allowed"] is True

    def test_deny_explanation_mentions_deny_list(self) -> None:
        result = parse(run(policy_probe(
            tool_name="write_file",
            policy_override={"deny_list": ["write_file"]},
            explain=True,
        )))
        assert "deny_list" in result["explanation"].lower()

    def test_matched_rule_is_deny_list(self) -> None:
        result = parse(run(policy_probe(
            tool_name="delete_file",
            policy_override={"deny_list": ["delete_file"]},
        )))
        assert result["matched_rule"] == "deny_list"


class TestPolicyProbeAllowList:
    """policy_override with allow_list (non-FS tools only to avoid path_arg bug)."""

    def test_allows_listed_tool(self) -> None:
        result = parse(run(policy_probe(
            tool_name="web_search",
            policy_override={"allow_list": ["web_search", "run_command"]},
        )))
        assert result["status"] == "ok"
        assert result["allowed"] is True

    def test_denies_unlisted_tool(self) -> None:
        result = parse(run(policy_probe(
            tool_name="shell_exec",
            policy_override={"allow_list": ["web_search"]},
        )))
        assert result["allowed"] is False

    def test_deny_explanation_mentions_allow_list(self) -> None:
        result = parse(run(policy_probe(
            tool_name="shell_exec",
            policy_override={"allow_list": ["web_search"]},
            explain=True,
        )))
        assert "allow_list" in result["explanation"].lower()

    def test_alternatives_provided_when_denied(self) -> None:
        result = parse(run(policy_probe(
            tool_name="shell_exec",
            policy_override={"allow_list": ["web_search", "run_command"]},
        )))
        assert result["allowed"] is False
        # alternatives should list permitted tools
        assert "alternatives" in result
        assert "web_search" in result["alternatives"] or "run_command" in result["alternatives"]

    def test_no_alternatives_when_allowed(self) -> None:
        result = parse(run(policy_probe(
            tool_name="web_search",
            policy_override={"allow_list": ["web_search"]},
        )))
        assert result["allowed"] is True
        assert "alternatives" not in result


class TestPolicyProbeNoExplain:
    def test_no_explanation_key_when_explain_false(self) -> None:
        result = parse(run(policy_probe(
            tool_name="web_search",
            policy_override={"full_access": True},
            explain=False,
        )))
        assert "explanation" not in result


class TestPolicyProbeDefaultPermissive:
    """When no DB and no policy_override, falls back to default_permissive (full_access)."""

    def test_default_is_permissive(self, tmp_path: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_path / "supervisor.db",
        ):
            result = parse(run(policy_probe(tool_name="anything")))
        assert result["status"] == "ok"
        assert result["allowed"] is True
        assert result["policy_source"] == "default_permissive"

    def test_default_policy_source(self, tmp_path: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_path / "supervisor.db",
        ):
            result = parse(run(policy_probe(tool_name="shell_exec")))
        assert result["policy_source"] == "default_permissive"


class TestPolicyProbeWithDb:
    """Load policy from the supervisor DB."""

    def test_loads_policy_from_db(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(policy_probe(tool_name="read_file", session_id="sess-1")))
        assert result["status"] == "ok"
        # Policy from DB: allow_list = ["read_file", "search_files"]
        assert result["allowed"] is True

    def test_denies_tool_not_in_db_allow_list(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(policy_probe(tool_name="write_file", session_id="sess-1")))
        assert result["allowed"] is False

    def test_policy_source_from_db(self, tmp_db: Path) -> None:
        with patch(
            "obscura.tools.system.intelligence._get_supervisor_db",
            return_value=tmp_db,
        ):
            result = parse(run(policy_probe(tool_name="read_file", session_id="sess-1")))
        assert result["policy_source"].startswith("db:")


class TestPolicyProbeMetadata:
    """Validate the metadata fields in every response."""

    def test_tool_name_echoed(self) -> None:
        result = parse(run(policy_probe(
            tool_name="my_tool",
            policy_override={"full_access": True},
        )))
        assert result["tool_name"] == "my_tool"

    def test_is_filesystem_tool_false_for_non_fs(self) -> None:
        result = parse(run(policy_probe(
            tool_name="web_search",
            policy_override={"full_access": True},
        )))
        assert result["is_filesystem_tool"] is False

    def test_is_filesystem_tool_true_for_fs(self) -> None:
        result = parse(run(policy_probe(
            tool_name="read_file",
            policy_override={"full_access": True},
        )))
        assert result["is_filesystem_tool"] is True

    def test_path_checked_none_for_non_fs_tool(self) -> None:
        result = parse(run(policy_probe(
            tool_name="web_search",
            policy_override={"full_access": True},
            args={"query": "something"},
        )))
        assert result["path_checked"] is None

    def test_path_checked_for_fs_tool(self) -> None:
        result = parse(run(policy_probe(
            tool_name="read_file",
            policy_override={"full_access": True},
            args={"path": "/tmp/foo.txt"},
        )))
        assert result["path_checked"] == "/tmp/foo.txt"

    def test_policy_summary_keys_present(self) -> None:
        result = parse(run(policy_probe(
            tool_name="any_tool",
            policy_override={"full_access": True},
        )))
        summary = result["policy_summary"]
        assert "full_access" in summary
        assert "allow_list" in summary
        assert "deny_list" in summary
        assert "base_dir" in summary

    def test_policy_summary_allow_list_sorted(self) -> None:
        result = parse(run(policy_probe(
            tool_name="read_file",
            policy_override={"allow_list": ["z_tool", "a_tool", "m_tool"]},
        )))
        al = result["policy_summary"]["allow_list"]
        assert al == sorted(al)

    def test_result_is_valid_json(self) -> None:
        raw = run(policy_probe(
            tool_name="any_tool",
            policy_override={"full_access": True},
        ))
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Tool spec registration
# ---------------------------------------------------------------------------


class TestToolRegistration:
    """Verify all 3 tools are registered in get_system_tool_specs()."""

    def test_context_snapshot_registered(self) -> None:
        from obscura.tools.system import get_system_tool_specs
        names = {s.name for s in get_system_tool_specs()}
        assert "context_snapshot" in names

    def test_causal_trace_registered(self) -> None:
        from obscura.tools.system import get_system_tool_specs
        names = {s.name for s in get_system_tool_specs()}
        assert "causal_trace" in names

    def test_policy_probe_registered(self) -> None:
        from obscura.tools.system import get_system_tool_specs
        names = {s.name for s in get_system_tool_specs()}
        assert "policy_probe" in names

    def test_all_three_have_spec_attribute(self) -> None:
        from obscura.tools.system.intelligence import causal_trace, context_snapshot, policy_probe
        assert hasattr(context_snapshot, "spec")
        assert hasattr(causal_trace, "spec")
        assert hasattr(policy_probe, "spec")

    def test_spec_names_match(self) -> None:
        from obscura.tools.system.intelligence import causal_trace, context_snapshot, policy_probe
        assert context_snapshot.spec.name == "context_snapshot"  # type: ignore[attr-defined]
        assert causal_trace.spec.name == "causal_trace"  # type: ignore[attr-defined]
        assert policy_probe.spec.name == "policy_probe"  # type: ignore[attr-defined]

    def test_spec_descriptions_non_empty(self) -> None:
        from obscura.tools.system.intelligence import causal_trace, context_snapshot, policy_probe
        assert context_snapshot.spec.description  # type: ignore[attr-defined]
        assert causal_trace.spec.description  # type: ignore[attr-defined]
        assert policy_probe.spec.description  # type: ignore[attr-defined]
