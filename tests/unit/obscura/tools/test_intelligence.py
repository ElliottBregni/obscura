"""Unit tests for intelligence tools — context_snapshot, causal_trace, policy_probe.

All three tools are async.

Mock strategy:
  - context_snapshot / causal_trace: patch resolve_obscura_home to tmp_path,
    create a real (writable) SQLite DB with the required tables.
  - policy_probe: uses policy_override parameter → no DB needed for most tests.
    DB-backed tests use the same SQLite helper.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import obscura.tools.system.intelligence as _intel_mod

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# SQLite helper — build a minimal supervisor.db
# ---------------------------------------------------------------------------


def _make_db(db_path: Path, *, sessions: list[dict] | None = None, events: list[dict] | None = None) -> None:  # type: ignore[type-arg]
    """Create a minimal supervisor.db with required tables and optional rows."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS supervisor_runs (
            session_id TEXT, run_id TEXT, started_at TEXT
        );
        CREATE TABLE IF NOT EXISTS supervisor_events (
            run_id TEXT, seq INTEGER, kind TEXT, payload_json TEXT, timestamp TEXT
        );
        CREATE TABLE IF NOT EXISTS tool_registrations (
            run_id TEXT, tool_name TEXT, tool_hash TEXT, registered_at TEXT
        );
        CREATE TABLE IF NOT EXISTS memory_items (
            key TEXT, content TEXT, importance REAL, recency REAL,
            relevance REAL, pinned INTEGER, source_run_id TEXT
        );
        CREATE TABLE IF NOT EXISTS memory_commits (
            run_id TEXT, committed INTEGER, deduplicated INTEGER,
            gated INTEGER, errors INTEGER, committed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS prompt_snapshots (
            run_id TEXT, assembled_at TEXT
        );
        CREATE TABLE IF NOT EXISTS policy_versions (
            session_id TEXT, version_id TEXT, created_at TEXT, policy_json TEXT
        );
        CREATE TABLE IF NOT EXISTS session_heartbeats (
            session_id TEXT, seq INTEGER, state TEXT,
            turn_number INTEGER, elapsed_ms INTEGER, timestamp TEXT
        );
        CREATE TABLE IF NOT EXISTS session_hooks (
            session_id TEXT, hook_id TEXT, hook_point TEXT,
            handler_name TEXT, enabled INTEGER, registered_at TEXT
        );
        """
    )
    for s in sessions or []:
        conn.execute(
            "INSERT INTO supervisor_runs VALUES (?,?,?)",
            (s["session_id"], s["run_id"], s.get("started_at", "2026-01-01")),
        )
    for e in events or []:
        conn.execute(
            "INSERT INTO supervisor_events VALUES (?,?,?,?,?)",
            (
                e["run_id"],
                e["seq"],
                e["kind"],
                e.get("payload_json", "{}"),
                e.get("timestamp", "2026-01-01T00:00:00"),
            ),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# context_snapshot
# ---------------------------------------------------------------------------


async def test_context_snapshot_no_db_returns_no_db_status(tmp_path: Path) -> None:
    from obscura.tools.system.intelligence import context_snapshot

    with patch.object(_intel_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await context_snapshot())

    assert result["status"] == "no_db"
    assert "supervisor.db" in result["message"].lower() or "not found" in result["message"].lower()


async def test_context_snapshot_empty_db_returns_ok_empty_ids(tmp_path: Path) -> None:
    from obscura.tools.system.intelligence import context_snapshot

    _make_db(tmp_path / "supervisor.db")

    with patch.object(_intel_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await context_snapshot())

    assert result["status"] == "ok"
    assert result["session_id"] == ""
    assert result["run_id"] == ""


async def test_context_snapshot_resolves_session_and_run(tmp_path: Path) -> None:
    from obscura.tools.system.intelligence import context_snapshot

    _make_db(
        tmp_path / "supervisor.db",
        sessions=[{"session_id": "sess-1", "run_id": "run-1"}],
    )

    with patch.object(_intel_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await context_snapshot())

    assert result["status"] == "ok"
    assert result["session_id"] == "sess-1"
    assert result["run_id"] == "run-1"


async def test_context_snapshot_include_filter_omits_other_sections(tmp_path: Path) -> None:
    from obscura.tools.system.intelligence import context_snapshot

    _make_db(
        tmp_path / "supervisor.db",
        sessions=[{"session_id": "s", "run_id": "r"}],
    )

    with patch.object(_intel_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await context_snapshot(include=["tools"]))

    assert result["status"] == "ok"
    assert "tools" in result
    assert "memory" not in result
    assert "heartbeats" not in result


async def test_context_snapshot_explicit_session_id(tmp_path: Path) -> None:
    from obscura.tools.system.intelligence import context_snapshot

    _make_db(
        tmp_path / "supervisor.db",
        sessions=[{"session_id": "explicit-sess", "run_id": "explicit-run"}],
    )

    with patch.object(_intel_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await context_snapshot(session_id="explicit-sess"))

    assert result["status"] == "ok"
    assert result["session_id"] == "explicit-sess"


# ---------------------------------------------------------------------------
# causal_trace
# ---------------------------------------------------------------------------


async def test_causal_trace_no_db_returns_no_db_status(tmp_path: Path) -> None:
    from obscura.tools.system.intelligence import causal_trace

    with patch.object(_intel_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await causal_trace())

    assert result["status"] == "no_db"


async def test_causal_trace_no_runs_returns_no_events(tmp_path: Path) -> None:
    from obscura.tools.system.intelligence import causal_trace

    _make_db(tmp_path / "supervisor.db")

    with patch.object(_intel_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await causal_trace())

    assert result["status"] == "no_events"


async def test_causal_trace_with_events_returns_chain(tmp_path: Path) -> None:
    from obscura.tools.system.intelligence import causal_trace

    _make_db(
        tmp_path / "supervisor.db",
        sessions=[{"session_id": "s", "run_id": "r"}],
        events=[
            {"run_id": "r", "seq": 1, "kind": "run_started"},
            {"run_id": "r", "seq": 2, "kind": "tool_execution_start"},
            {"run_id": "r", "seq": 3, "kind": "tool_execution_end"},
        ],
    )

    with patch.object(_intel_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await causal_trace(run_id="r", session_id="s"))

    assert result["status"] == "ok"
    assert result["chain_length"] >= 1
    assert result["total_events_in_run"] == 3


async def test_causal_trace_outcome_finds_matching_event(tmp_path: Path) -> None:
    from obscura.tools.system.intelligence import causal_trace

    _make_db(
        tmp_path / "supervisor.db",
        sessions=[{"session_id": "s", "run_id": "r"}],
        events=[
            {"run_id": "r", "seq": 1, "kind": "run_started"},
            {"run_id": "r", "seq": 2, "kind": "drift_detected"},
        ],
    )

    with patch.object(_intel_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await causal_trace(run_id="r", session_id="s", outcome="drift"))

    assert result["status"] == "ok"
    assert result["terminal_event"] == "drift_detected"


async def test_causal_trace_include_payloads_adds_payload_field(tmp_path: Path) -> None:
    from obscura.tools.system.intelligence import causal_trace

    _make_db(
        tmp_path / "supervisor.db",
        sessions=[{"session_id": "s", "run_id": "r"}],
        events=[
            {"run_id": "r", "seq": 1, "kind": "run_started", "payload_json": '{"info": 1}'},
        ],
    )

    with patch.object(_intel_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await causal_trace(run_id="r", session_id="s", include_payloads=True))

    assert result["status"] == "ok"
    assert result["trace"][0]["payload"] == {"info": 1}


# ---------------------------------------------------------------------------
# policy_probe — uses policy_override, no DB needed
# ---------------------------------------------------------------------------


async def test_policy_probe_full_access_allows_any_tool() -> None:
    from obscura.tools.system.intelligence import policy_probe

    result = json.loads(
        await policy_probe(
            tool_name="write_text_file",
            policy_override={"full_access": True},
        )
    )

    assert result["status"] == "ok"
    assert result["allowed"] is True
    assert result["policy_source"] == "inline_override"


async def test_policy_probe_deny_list_blocks_tool() -> None:
    from obscura.tools.system.intelligence import policy_probe

    result = json.loads(
        await policy_probe(
            tool_name="write_text_file",
            policy_override={"deny_list": ["write_text_file"]},
        )
    )

    assert result["status"] == "ok"
    assert result["allowed"] is False
    assert result["matched_rule"] == "deny_list"


async def test_policy_probe_allow_list_excludes_unlisted_tool() -> None:
    from obscura.tools.system.intelligence import policy_probe

    result = json.loads(
        await policy_probe(
            tool_name="shell_exec",
            policy_override={"allow_list": ["read_text_file"]},
        )
    )

    assert result["status"] == "ok"
    assert result["allowed"] is False


async def test_policy_probe_invalid_policy_override_returns_error() -> None:
    from obscura.tools.system.intelligence import policy_probe

    result = json.loads(
        await policy_probe(
            tool_name="anything",
            policy_override="not_a_dict",  # type: ignore[arg-type]
        )
    )

    assert result["status"] == "error"


async def test_policy_probe_no_db_falls_back_to_permissive(tmp_path: Path) -> None:
    """No DB → default_permissive policy → everything allowed."""
    from obscura.tools.system.intelligence import policy_probe

    with patch.object(_intel_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await policy_probe(tool_name="any_tool"))

    assert result["status"] == "ok"
    assert result["allowed"] is True
    assert result["policy_source"] == "default_permissive"


async def test_policy_probe_explanation_included_by_default() -> None:
    from obscura.tools.system.intelligence import policy_probe

    result = json.loads(
        await policy_probe(
            tool_name="my_tool",
            policy_override={"full_access": True},
        )
    )

    assert "explanation" in result
    assert isinstance(result["explanation"], str)
    assert len(result["explanation"]) > 0
