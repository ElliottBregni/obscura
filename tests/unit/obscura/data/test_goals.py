"""Tests for the goal-board repository wrapper."""

from __future__ import annotations

from pathlib import Path

import pytest

from obscura.data.goals import Goal, GoalRepo, get_goal_repo


@pytest.mark.unit
class TestGoalRepoFactory:
    def test_factory_returns_goal_repo(self, tmp_path: Path) -> None:
        repo = get_goal_repo(goals_dir=tmp_path / "goals")
        assert isinstance(repo, GoalRepo)

    def test_protocol_methods_present(self, tmp_path: Path) -> None:
        repo = get_goal_repo(goals_dir=tmp_path / "goals")
        for method in (
            "load",
            "load_all",
            "active_goals",
            "active_summary",
            "get_if_newer",
            "create",
            "update",
        ):
            assert hasattr(repo, method), method

    def test_create_load_round_trip(self, tmp_path: Path) -> None:
        repo = get_goal_repo(goals_dir=tmp_path / "goals")
        goal = repo.create(
            "ship the data layer",
            priority="high",
            acceptance_criteria=["events", "tasks", "goals"],
        )
        assert isinstance(goal, Goal)
        assert goal.title == "ship the data layer"
        assert goal.priority == "high"
        loaded = repo.load(goal.id)
        assert loaded is not None
        assert loaded.title == goal.title
        assert loaded.acceptance_criteria == ("events", "tasks", "goals")

    def test_active_summary_empty_store(self, tmp_path: Path) -> None:
        repo = get_goal_repo(goals_dir=tmp_path / "goals")
        assert repo.active_summary() == ""

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        repo = get_goal_repo(goals_dir=tmp_path / "goals")
        assert repo.load("does-not-exist") is None
