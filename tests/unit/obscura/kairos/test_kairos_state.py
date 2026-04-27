"""Tests for obscura.kairos.state — KairosState persistence and cap enforcement."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from obscura.kairos.state import KairosState


# ---------------------------------------------------------------------------
# add_project_root / project_roots_seen
# ---------------------------------------------------------------------------


def test_add_project_root_basic() -> None:
    state = KairosState()
    state.add_project_root("/home/user/proj")
    assert state.project_roots_seen == ["/home/user/proj"]


def test_add_project_root_deduplication() -> None:
    state = KairosState()
    state.add_project_root("/home/user/proj")
    state.add_project_root("/home/user/proj")
    state.add_project_root("/home/user/proj")
    assert state.project_roots_seen == ["/home/user/proj"]


def test_add_project_root_cap_at_100() -> None:
    state = KairosState()
    for i in range(110):
        state.add_project_root(f"/proj/{i}")
    assert len(state.project_roots_seen) == 100
    # The 10 oldest entries (0–9) should have been evicted
    assert "/proj/0" not in state.project_roots_seen
    assert "/proj/9" not in state.project_roots_seen
    # The 100 newest entries (10–109) should still be present
    assert "/proj/10" in state.project_roots_seen
    assert "/proj/109" in state.project_roots_seen


def test_add_project_root_ignores_empty_string() -> None:
    state = KairosState()
    state.add_project_root("")
    assert state.project_roots_seen == []


def test_record_project_delegates_to_add_project_root() -> None:
    """record_project() is a backward-compatible wrapper."""
    state = KairosState()
    for i in range(110):
        state.record_project(f"/proj/{i}")
    assert len(state.project_roots_seen) == 100


# ---------------------------------------------------------------------------
# record_error / common_errors
# ---------------------------------------------------------------------------


def test_record_error_increments_count() -> None:
    state = KairosState()
    state.record_error("TimeoutError")
    state.record_error("TimeoutError")
    assert state.common_errors["TimeoutError"] == 2


def test_record_error_cap_at_50() -> None:
    state = KairosState()
    # Add 51 distinct errors, each with count 1
    for i in range(51):
        state.record_error(f"error_{i}")
    assert len(state.common_errors) == 50


def test_record_error_retains_highest_frequency() -> None:
    state = KairosState()
    # Seed 50 errors each with count 1
    for i in range(50):
        state.record_error(f"rare_{i}")
    # Add a new error that triggers the cap; all existing errors have count 1,
    # so one of them will be dropped (not the high-frequency one we add next).
    state.record_error("frequent_error")
    state.record_error("frequent_error")
    # frequent_error has count 2 — it must survive the cap
    assert "frequent_error" in state.common_errors
    assert state.common_errors["frequent_error"] == 2
    assert len(state.common_errors) == 50


# ---------------------------------------------------------------------------
# record_log_entry / last_log_date
# ---------------------------------------------------------------------------


def test_record_log_entry_increments_total() -> None:
    state = KairosState()
    state.record_log_entry()
    state.record_log_entry()
    assert state.total_log_entries == 2


def test_record_log_entry_sets_date_only() -> None:
    state = KairosState()
    state.record_log_entry()
    # Must be YYYY-MM-DD only, no time component
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", state.last_log_date)


# ---------------------------------------------------------------------------
# load() — cap enforcement on existing bloated state files
# ---------------------------------------------------------------------------


def test_load_enforces_project_roots_cap(tmp_path: Path) -> None:
    state_file = tmp_path / "kairos_state.json"
    bloated = {
        "project_roots_seen": [f"/proj/{i}" for i in range(200)],
        "common_errors": {},
    }
    state_file.write_text(json.dumps(bloated), encoding="utf-8")

    state = KairosState.load(state_file)
    assert len(state.project_roots_seen) == 100
    # Should keep the last 100 (newest)
    assert "/proj/100" in state.project_roots_seen
    assert "/proj/199" in state.project_roots_seen
    assert "/proj/99" not in state.project_roots_seen


def test_load_deduplicates_project_roots(tmp_path: Path) -> None:
    state_file = tmp_path / "kairos_state.json"
    # 50 unique roots but each duplicated
    roots = [r for r in [f"/proj/{i}" for i in range(50)] for _ in range(2)]
    bloated = {"project_roots_seen": roots, "common_errors": {}}
    state_file.write_text(json.dumps(bloated), encoding="utf-8")

    state = KairosState.load(state_file)
    assert len(state.project_roots_seen) == 50


def test_load_enforces_common_errors_cap(tmp_path: Path) -> None:
    state_file = tmp_path / "kairos_state.json"
    bloated = {
        "project_roots_seen": [],
        "common_errors": {f"err_{i}": i + 1 for i in range(100)},
    }
    state_file.write_text(json.dumps(bloated), encoding="utf-8")

    state = KairosState.load(state_file)
    assert len(state.common_errors) == 50
    # Highest-frequency errors must be retained
    assert "err_99" in state.common_errors  # count 100, highest
    assert "err_50" in state.common_errors  # count 51


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    state = KairosState.load(tmp_path / "nonexistent.json")
    assert state.total_sessions == 0
    assert state.project_roots_seen == []
    assert state.common_errors == {}


def test_load_ignores_unknown_fields(tmp_path: Path) -> None:
    state_file = tmp_path / "kairos_state.json"
    state_file.write_text(
        json.dumps({"total_sessions": 7, "unknown_future_field": "value"}),
        encoding="utf-8",
    )
    state = KairosState.load(state_file)
    assert state.total_sessions == 7
