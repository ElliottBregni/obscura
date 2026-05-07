"""Unit tests for goal_tool — GoalBoard CRUD.

goal_tool is a **sync** function, so tests use plain ``def test_*``.
All side-effect helpers (_notify_vault, _notify_arbiter, _emit_goal_event)
are monkeypatched to no-ops.  The GoalBoard itself is replaced per-test
by a MagicMock configured with a minimal ``@dataclass`` Goal stand-in so
that ``dataclasses.asdict`` (used internally by _goal_dict) works correctly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Minimal goal dataclass — asdict() must succeed
# ---------------------------------------------------------------------------


@dataclass
class _Goal:
    """Mimics the fields accessed by goal_tool / _goal_dict."""

    id: str
    title: str
    status: str = "active"
    priority: str = "medium"
    context: str = ""
    acceptance_criteria: tuple[str, ...] = dc_field(default_factory=tuple)
    depends_on: tuple[str, ...] = dc_field(default_factory=tuple)
    tasks: tuple[str, ...] = dc_field(default_factory=tuple)
    progress: int | None = None
    last_worked: str | None = None
    path: str = "/mock/goals/goal.md"  # stripped by _goal_dict


# ---------------------------------------------------------------------------
# Autouse: no-op all external side effects
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _suppress_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("obscura.tools.goal_tools._notify_vault", lambda *a: None)
    monkeypatch.setattr("obscura.tools.goal_tools._notify_arbiter", lambda *a: None)
    monkeypatch.setattr(
        "obscura.tools.goal_tools._emit_goal_event", lambda *a, **kw: None
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_board(**methods: Any) -> MagicMock:
    """Return a MagicMock board with specified method return values."""
    board = MagicMock()
    for name, value in methods.items():
        getattr(board, name).return_value = value
    return board


# ---------------------------------------------------------------------------
# action="create"
# ---------------------------------------------------------------------------


def test_goal_create_returns_goal_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from obscura.tools.goal_tools import goal_tool

    g = _Goal(id="goal-abc", title="Ship v2")
    board = _make_board(create=g)
    monkeypatch.setattr("obscura.tools.goal_tools._board", lambda: board)

    result = json.loads(goal_tool(action="create", title="Ship v2"))

    assert result["ok"] is True
    assert result["goal_id"] == "goal-abc"
    assert result["goal"]["title"] == "Ship v2"
    assert "path" not in result["goal"]  # _goal_dict strips path


def test_goal_create_missing_title_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from obscura.tools.goal_tools import goal_tool

    monkeypatch.setattr("obscura.tools.goal_tools._board", lambda: MagicMock())

    result = json.loads(goal_tool(action="create", title=""))

    assert result["ok"] is False
    assert "missing_title" in result["error"]


def test_goal_create_strips_path_from_returned_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from obscura.tools.goal_tools import goal_tool

    g = _Goal(id="g-1", title="T", path="/secret/path.md")
    monkeypatch.setattr("obscura.tools.goal_tools._board", lambda: _make_board(create=g))

    result = json.loads(goal_tool(action="create", title="T"))

    assert "path" not in result["goal"]


# ---------------------------------------------------------------------------
# action="list"
# ---------------------------------------------------------------------------


def test_goal_list_returns_all_goals(monkeypatch: pytest.MonkeyPatch) -> None:
    from obscura.tools.goal_tools import goal_tool

    goals = [_Goal("g-1", "Goal A"), _Goal("g-2", "Goal B")]
    monkeypatch.setattr(
        "obscura.tools.goal_tools._board", lambda: _make_board(load_all=goals)
    )

    result = json.loads(goal_tool(action="list"))

    assert result["ok"] is True
    assert result["count"] == 2
    titles = {g["title"] for g in result["goals"]}
    assert titles == {"Goal A", "Goal B"}


def test_goal_list_empty_board_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from obscura.tools.goal_tools import goal_tool

    monkeypatch.setattr(
        "obscura.tools.goal_tools._board", lambda: _make_board(load_all=[])
    )

    result = json.loads(goal_tool(action="list"))

    assert result["ok"] is True
    assert result["count"] == 0
    assert result["goals"] == []


def test_goal_list_status_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    from obscura.tools.goal_tools import goal_tool

    goals = [
        _Goal("g-1", "Active goal", status="active"),
        _Goal("g-2", "Done goal", status="completed"),
    ]
    monkeypatch.setattr(
        "obscura.tools.goal_tools._board", lambda: _make_board(load_all=goals)
    )

    result = json.loads(goal_tool(action="list", status="active"))

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["goals"][0]["title"] == "Active goal"


# ---------------------------------------------------------------------------
# action="get"
# ---------------------------------------------------------------------------


def test_goal_get_returns_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    from obscura.tools.goal_tools import goal_tool

    g = _Goal(id="g-99", title="Find me")
    monkeypatch.setattr(
        "obscura.tools.goal_tools._board", lambda: _make_board(load=g)
    )

    result = json.loads(goal_tool(action="get", goal_id="g-99"))

    assert result["ok"] is True
    assert result["goal"]["title"] == "Find me"


def test_goal_get_missing_goal_id_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from obscura.tools.goal_tools import goal_tool

    monkeypatch.setattr("obscura.tools.goal_tools._board", lambda: MagicMock())

    result = json.loads(goal_tool(action="get", goal_id=""))

    assert result["ok"] is False
    assert "missing_goal_id" in result["error"]


def test_goal_get_not_found_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from obscura.tools.goal_tools import goal_tool

    monkeypatch.setattr(
        "obscura.tools.goal_tools._board", lambda: _make_board(load=None)
    )

    result = json.loads(goal_tool(action="get", goal_id="no-such"))

    assert result["ok"] is False
    assert "goal_not_found" in result["error"]


# ---------------------------------------------------------------------------
# action="update"
# ---------------------------------------------------------------------------


def test_goal_update_title(monkeypatch: pytest.MonkeyPatch) -> None:
    from obscura.tools.goal_tools import goal_tool

    updated = _Goal(id="g-1", title="New title")
    monkeypatch.setattr(
        "obscura.tools.goal_tools._board", lambda: _make_board(update=updated)
    )

    result = json.loads(goal_tool(action="update", goal_id="g-1", title="New title"))

    assert result["ok"] is True
    assert result["goal"]["title"] == "New title"


def test_goal_update_not_found_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from obscura.tools.goal_tools import goal_tool

    monkeypatch.setattr(
        "obscura.tools.goal_tools._board", lambda: _make_board(update=None)
    )

    result = json.loads(goal_tool(action="update", goal_id="g-nope", title="X"))

    assert result["ok"] is False


# ---------------------------------------------------------------------------
# action="complete"
# ---------------------------------------------------------------------------


def test_goal_complete_returns_updated_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    from obscura.tools.goal_tools import goal_tool

    g = _Goal(id="g-1", title="Done", status="completed")
    monkeypatch.setattr(
        "obscura.tools.goal_tools._board", lambda: _make_board(complete=g)
    )

    result = json.loads(goal_tool(action="complete", goal_id="g-1"))

    assert result["ok"] is True
    assert result["goal"]["status"] == "completed"


# ---------------------------------------------------------------------------
# action="abandon"
# ---------------------------------------------------------------------------


def test_goal_abandon_returns_updated_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    from obscura.tools.goal_tools import goal_tool

    g = _Goal(id="g-1", title="Dropped", status="abandoned")
    monkeypatch.setattr(
        "obscura.tools.goal_tools._board", lambda: _make_board(abandon=g)
    )

    result = json.loads(goal_tool(action="abandon", goal_id="g-1", reason="no longer needed"))

    assert result["ok"] is True
    assert result["goal"]["status"] == "abandoned"


# ---------------------------------------------------------------------------
# action="add_task"
# ---------------------------------------------------------------------------


def test_goal_add_task_links_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from obscura.tools.goal_tools import goal_tool

    g = _Goal(id="g-1", title="Task goal", tasks=("task-99",))
    monkeypatch.setattr(
        "obscura.tools.goal_tools._board", lambda: _make_board(link_task=g)
    )

    result = json.loads(goal_tool(action="add_task", goal_id="g-1", task_id="task-99"))

    assert result["ok"] is True


def test_goal_add_task_missing_params_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from obscura.tools.goal_tools import goal_tool

    monkeypatch.setattr("obscura.tools.goal_tools._board", lambda: MagicMock())

    result = json.loads(goal_tool(action="add_task", goal_id="g-1", task_id=""))

    assert result["ok"] is False
    assert "missing_params" in result["error"]


# ---------------------------------------------------------------------------
# Unknown action
# ---------------------------------------------------------------------------


def test_unknown_action_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from obscura.tools.goal_tools import goal_tool

    monkeypatch.setattr("obscura.tools.goal_tools._board", lambda: MagicMock())

    result = json.loads(goal_tool(action="fly_to_mars"))

    assert result["ok"] is False
    assert "invalid_action" in result["error"]


# ---------------------------------------------------------------------------
# Tool spec registration
# ---------------------------------------------------------------------------


def test_get_goal_tool_specs_returns_one_spec() -> None:
    from obscura.tools.goal_tools import get_goal_tool_specs

    specs = get_goal_tool_specs()

    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "goal"
    assert callable(spec.handler)
    assert isinstance(spec.parameters, dict)
    assert spec.parameters.get("type") == "object"
