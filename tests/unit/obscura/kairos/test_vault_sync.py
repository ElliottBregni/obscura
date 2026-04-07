"""Unit tests for obscura.kairos.vault_sync."""

from __future__ import annotations

from pathlib import Path

import pytest

from obscura.kairos.vault_sync import VaultSync


def test_bootstrap_creates_zones(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vs = VaultSync(vault_dir=vault)
    vs.bootstrap()

    assert (vault / "user" / "goals").is_dir()
    assert (vault / "user" / "tasks").is_dir()
    assert (vault / "user" / "notes").is_dir()
    assert (vault / "agent" / "goals").is_dir()
    assert (vault / "agent" / "tasks").is_dir()
    assert (vault / "agent" / "arbiter").is_dir()
    assert (vault / "shared" / "decisions").is_dir()
    assert (vault / "shared" / "context").is_dir()
    assert (vault / "user" / "profile.md").exists()


def test_scan_and_detect_changes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vs = VaultSync(vault_dir=vault)
    vs.bootstrap()

    # Write a user note.
    f1 = vault / "user" / "notes" / "note1.md"
    f1.write_text("---\ntype: note\n---\n\nhello world")
    f2 = vault / "shared" / "context" / "shared.md"
    f2.write_text("---\ntype: note\n---\n\nshared content")

    metas = vs.scan()
    names = {m.path.name for m in metas}
    assert "note1.md" in names
    assert "shared.md" in names
    # profile.md was seeded by bootstrap
    assert "profile.md" in names

    # All files should be detected as "added" (no previous state).
    changes = vs.detect_changes()
    assert len(changes.added) >= 3  # noqa: PLR2004  (note1 + shared + profile)
    assert len(changes.modified) == 0
    assert len(changes.removed) == 0


def test_scan_respects_zones(tmp_path: Path) -> None:
    """Files outside zone dirs (e.g. obsidian/) are excluded."""
    vault = tmp_path / "vault"
    vs = VaultSync(vault_dir=vault)
    vs.bootstrap()

    # File in a non-zone directory.
    obsidian = vault / "obsidian"
    obsidian.mkdir()
    (obsidian / "Welcome.md").write_text("# Welcome")

    metas = vs.scan()
    assert all(m.owner in ("user", "agent", "shared") for m in metas)
    assert not any(m.path.name == "Welcome.md" for m in metas)


def test_detect_changes_modified(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vs = VaultSync(vault_dir=vault)
    vs.bootstrap()

    note = vault / "user" / "notes" / "changeme.md"
    note.write_text("---\ntype: note\n---\n\noriginal")

    # Record initial state.
    for m in vs.scan():
        vs._prev_hashes[str(m.path)] = m.hash

    # Modify the file.
    note.write_text("---\ntype: note\n---\n\nupdated content")

    changes = vs.detect_changes()
    assert any(m.path.name == "changeme.md" for m in changes.modified)


def test_frontmatter_parsing(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vs = VaultSync(vault_dir=vault)
    vs.bootstrap()

    goal = vault / "user" / "goals" / "ship-auth.md"
    goal.write_text(
        "---\ntype: goal\npriority: high\ntitle: Ship Auth\n"
        "acceptance_criteria:\n  - SSO works\n  - Tests pass\n---\n\n"
        "Context about the auth system."
    )

    metas = vs.scan("user")
    goal_meta = next(m for m in metas if m.path.name == "ship-auth.md")
    assert goal_meta.frontmatter["type"] == "goal"
    assert goal_meta.frontmatter["priority"] == "high"
    assert goal_meta.frontmatter["title"] == "Ship Auth"
    assert goal_meta.frontmatter["acceptance_criteria"] == ["SSO works", "Tests pass"]
    assert "Context about the auth system" in goal_meta.body


def test_ingest_goal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ingesting a goal file creates a GoalBoard entry."""
    vault = tmp_path / "vault"
    goals_dir = tmp_path / "goals"
    db_file = tmp_path / "tasks.db"

    monkeypatch.setattr("obscura.core.task_queue._db_path", lambda: db_file)
    # Redirect GoalBoard's default dir to tmp.
    monkeypatch.setattr("obscura.kairos.goals._GOALS_DIR", goals_dir)

    vs = VaultSync(vault_dir=vault)
    vs.bootstrap()

    # Write a goal to user zone.
    goal_file = vault / "user" / "goals" / "ship-auth.md"
    goal_file.write_text(
        "---\ntype: goal\npriority: high\ntitle: Ship Auth\n"
        "acceptance_criteria:\n  - SSO works\n---\n\n"
        "Ship the new auth flow."
    )

    meta = vs.scan("user/goals")[0]
    vs._ingest_file(meta)

    from obscura.kairos.goals import GoalBoard

    board = GoalBoard(goals_dir=goals_dir)
    goals = board.load_all()
    assert len(goals) == 1
    assert goals[0].title == "Ship Auth"
    assert goals[0].priority == "high"


def test_ingest_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ingesting a task file creates a TaskQueue entry."""
    vault = tmp_path / "vault"
    db_file = tmp_path / "tasks.db"
    monkeypatch.setattr("obscura.core.task_queue._db_path", lambda: db_file)

    vs = VaultSync(vault_dir=vault)
    vs.bootstrap()

    task_file = vault / "user" / "tasks" / "fix-login.md"
    task_file.write_text(
        "---\ntype: task\npriority: high\ntitle: Fix Login Bug\n---\n\n"
        "The login form throws a 500 on submit."
    )

    meta = vs.scan("user/tasks")[0]
    vs._ingest_file(meta)

    from obscura.core.task_queue import TaskQueue

    q = TaskQueue()
    task = q.next_ready()
    assert task is not None
    assert task["subject"] == "Fix Login Bug"
    assert task["priority"] == 25  # high → 25


def test_export_goals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exporting goals writes markdown to agent/goals/."""
    vault = tmp_path / "vault"
    goals_dir = tmp_path / "goals"

    monkeypatch.setattr("obscura.kairos.goals._GOALS_DIR", goals_dir)

    vs = VaultSync(vault_dir=vault)
    vs.bootstrap()

    from obscura.kairos.goals import GoalBoard

    board = GoalBoard(goals_dir=goals_dir)
    board.create("Test Goal", priority="high", context="Some context")

    count = vs._export_goals()
    assert count == 1
    exported = list((vault / "agent" / "goals").glob("*.md"))
    assert len(exported) == 1
    content = exported[0].read_text()
    assert "Test Goal" in content
    assert "high" in content


def test_status(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vs = VaultSync(vault_dir=vault)
    vs.bootstrap()

    (vault / "user" / "notes" / "a.md").write_text("hello")

    status = vs.status()
    assert status["exists"] is True
    assert status["zones"]["user"] >= 1


def test_notify_functions_dont_crash() -> None:
    """Notify helpers should be no-ops when vault doesn't exist."""
    from obscura.kairos.vault_sync import notify_goal_changed, notify_profile_changed

    # These should not raise even with no vault.
    notify_goal_changed("nonexistent")
    notify_profile_changed()
