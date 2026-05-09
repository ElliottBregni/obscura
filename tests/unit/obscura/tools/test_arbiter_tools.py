"""Unit tests for arbiter_tools — arbiter_status, arbiter_appeal, query_arbiter_verdicts.

All three tools are async.

Mock strategy:
  - arbiter_status / arbiter_appeal: patch get_engine at the import site
    (obscura.tools.arbiter_tools.get_engine), not at obscura.arbiter.hooks,
    because the module uses a direct `from X import get_engine` binding.
  - query_arbiter_verdicts: patch resolve_obscura_home at the module level so
    the SQLite lookup hits tmp_path instead of ~/.obscura.
  - arbiter_appeal calls `from dataclasses import replace as dc_replace` inside
    the function body (local import), so patch dataclasses.replace directly.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import obscura.tools.arbiter_tools as _at_mod

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# arbiter_status
# ---------------------------------------------------------------------------


async def test_arbiter_status_no_engine_returns_not_running() -> None:
    from obscura.tools.arbiter_tools import arbiter_status

    with patch.object(_at_mod, "get_engine", return_value=None):
        result = json.loads(await arbiter_status())

    assert result["ok"] is True
    assert result["running"] is False


async def test_arbiter_status_with_engine_no_events_returns_ok() -> None:
    from obscura.tools.arbiter_tools import arbiter_status

    engine = MagicMock()
    engine.status.return_value = {"running": True, "score_dist": {}}
    engine.events = []

    with patch.object(_at_mod, "get_engine", return_value=engine):
        result = json.loads(await arbiter_status())

    assert result["ok"] is True
    assert result["recent_verdicts"] == []


async def test_arbiter_status_with_events_includes_verdicts() -> None:
    from obscura.tools.arbiter_tools import arbiter_status

    event = MagicMock()
    event.kind.value = "tool_call"
    event.verdict.value = "accept"
    event.target_id = "task:123"
    event.score.composite = 0.95
    event.score.feedback = "looks good"
    event.timestamp.isoformat.return_value = "2026-05-07T00:00:00"

    engine = MagicMock()
    engine.status.return_value = {"running": True}
    engine.events = [event]

    with patch.object(_at_mod, "get_engine", return_value=engine):
        result = json.loads(await arbiter_status(last_n=5))

    assert result["ok"] is True
    assert len(result["recent_verdicts"]) == 1
    assert result["recent_verdicts"][0]["verdict"] == "accept"
    assert result["recent_verdicts"][0]["target_id"] == "task:123"


async def test_arbiter_status_engine_exception_returns_error() -> None:
    from obscura.tools.arbiter_tools import arbiter_status

    with patch.object(_at_mod, "get_engine", side_effect=RuntimeError("boom")):
        result = json.loads(await arbiter_status())

    assert result["ok"] is False
    assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# arbiter_appeal
# ---------------------------------------------------------------------------


async def test_arbiter_appeal_no_engine_returns_error() -> None:
    from obscura.tools.arbiter_tools import arbiter_appeal

    with patch.object(_at_mod, "get_engine", return_value=None):
        result = json.loads(
            await arbiter_appeal(target_id="task:x", reasoning="please")
        )

    assert result["ok"] is False
    assert "not active" in result["error"].lower()


async def test_arbiter_appeal_no_deny_event_returns_error() -> None:
    from obscura.tools.arbiter_tools import arbiter_appeal

    engine = MagicMock()
    engine.events = []

    with patch.object(_at_mod, "get_engine", return_value=engine):
        result = json.loads(
            await arbiter_appeal(target_id="task:missing", reasoning="retry")
        )

    assert result["ok"] is False
    assert "No recent DENY" in result["error"]


async def test_arbiter_appeal_calls_evaluate_and_returns_verdict() -> None:
    from obscura.tools.arbiter_tools import arbiter_appeal

    event = MagicMock()
    event.target_id = "task:abc"
    event.verdict.value = "deny"
    event.kind = MagicMock()

    score = MagicMock()
    score.verdict.value = "accept"
    score.composite = 0.9
    score.feedback = "appeal accepted"

    engine = MagicMock()
    engine.events = [event]
    engine.evaluate = AsyncMock(return_value=score)
    engine._config = MagicMock()

    with (
        patch.object(_at_mod, "get_engine", return_value=engine),
        # dc_replace is imported inside the function body, so patch the stdlib directly
        patch("dataclasses.replace", return_value=MagicMock()),
    ):
        result = json.loads(
            await arbiter_appeal(target_id="task:abc", reasoning="please reconsider")
        )

    assert result["ok"] is True
    assert result["new_verdict"] == "accept"
    assert result["target_id"] == "task:abc"
    engine.evaluate.assert_awaited_once()


# ---------------------------------------------------------------------------
# query_arbiter_verdicts helpers
# ---------------------------------------------------------------------------


def _create_verdicts_db(db_path: Path, rows: list[dict]) -> None:  # type: ignore[type-arg]
    """Create a minimal arbiter.db with a verdicts table."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE verdicts (
            id TEXT,
            session_id TEXT,
            kind TEXT,
            verdict TEXT,
            composite REAL,
            created_at TEXT,
            details TEXT,
            metadata TEXT
        )"""
    )
    for i, row in enumerate(rows):
        conn.execute(
            "INSERT INTO verdicts VALUES (?,?,?,?,?,?,?,?)",
            (
                row.get("id", f"v{i}"),
                row.get("session_id", "sess1"),
                row.get("kind", "tool_call"),
                row.get("verdict", "accept"),
                row.get("composite", 0.9),
                row.get("created_at", f"2026-05-07T00:00:0{i}"),
                row.get("details", "{}"),
                row.get("metadata", "{}"),
            ),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# query_arbiter_verdicts
# ---------------------------------------------------------------------------


async def test_query_verdicts_no_db_returns_empty(tmp_path: Path) -> None:
    from obscura.tools.arbiter_tools import query_arbiter_verdicts

    with patch.object(_at_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await query_arbiter_verdicts())

    assert result["ok"] is True
    assert result["verdicts"] == []


async def test_query_verdicts_with_db_returns_rows(tmp_path: Path) -> None:
    from obscura.tools.arbiter_tools import query_arbiter_verdicts

    _create_verdicts_db(tmp_path / "arbiter.db", [{"id": "v1", "verdict": "accept"}])

    with patch.object(_at_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await query_arbiter_verdicts())

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["verdicts"][0]["verdict"] == "accept"


async def test_query_verdicts_filters_by_verdict_type(tmp_path: Path) -> None:
    from obscura.tools.arbiter_tools import query_arbiter_verdicts

    _create_verdicts_db(
        tmp_path / "arbiter.db",
        [{"id": "v1", "verdict": "accept"}, {"id": "v2", "verdict": "deny"}],
    )

    with patch.object(_at_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await query_arbiter_verdicts(verdict="deny"))

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["verdicts"][0]["verdict"] == "deny"


async def test_query_verdicts_filters_by_min_score(tmp_path: Path) -> None:
    from obscura.tools.arbiter_tools import query_arbiter_verdicts

    _create_verdicts_db(
        tmp_path / "arbiter.db",
        [{"id": "v1", "composite": 0.9}, {"id": "v2", "composite": 0.3}],
    )

    with patch.object(_at_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await query_arbiter_verdicts(min_score=0.5))

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["verdicts"][0]["composite"] >= 0.5


async def test_query_verdicts_limit_capped_at_100(tmp_path: Path) -> None:
    from obscura.tools.arbiter_tools import query_arbiter_verdicts

    _create_verdicts_db(tmp_path / "arbiter.db", [{"id": f"v{i}"} for i in range(5)])

    with patch.object(_at_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await query_arbiter_verdicts(limit=999))

    assert result["ok"] is True
    assert result["count"] <= 100


# ---------------------------------------------------------------------------
# Tool spec registration
# ---------------------------------------------------------------------------


def test_get_arbiter_tool_specs_returns_three() -> None:
    from obscura.tools.arbiter_tools import get_arbiter_tool_specs

    specs = get_arbiter_tool_specs()

    assert len(specs) == 3
    names = {s.name for s in specs}
    assert names == {"arbiter_status", "arbiter_appeal", "query_arbiter_verdicts"}
    for spec in specs:
        assert callable(spec.handler)
        assert isinstance(spec.parameters, dict)
