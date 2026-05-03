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
    # Isolate from any real kairos.db on disk so only GoalBoard goals are exported.
    monkeypatch.setattr(
        "obscura.core.paths.resolve_obscura_home", lambda cwd=None: tmp_path
    )

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


def test_ingest_goal_user_zone_wins_on_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """User zone always wins when a goal file is edited while GoalBoard has a newer version.

    Scenario:
      1. GoalBoard already has a goal (e.g. agent updated progress to 80%).
      2. User edits the vault/user/ markdown file with an older 'updated' timestamp.
      3. _ingest_goal() runs — user file wins, GoalBoard is overwritten.
      4. The old in-memory version is archived to vault/agent/goals/.conflicts/.
    """
    import logging

    vault = tmp_path / "vault"
    goals_dir = tmp_path / "goals"
    db_file = tmp_path / "tasks.db"

    monkeypatch.setattr("obscura.core.task_queue._db_path", lambda: db_file)
    monkeypatch.setattr("obscura.kairos.goals._GOALS_DIR", goals_dir)

    vs = VaultSync(vault_dir=vault)
    vs.bootstrap()

    # --- Step 1: seed GoalBoard with an in-memory version that has newer progress ---
    from obscura.kairos.goals import GoalBoard

    board = GoalBoard(goals_dir=goals_dir)
    agent_goal = board.create(
        "Ship Auth",
        priority="high",
        context="Agent context with 80% progress.",
        status="in_progress",
    )
    # Simulate agent writing newer progress (updated timestamp will be after creation).
    board.update(agent_goal.id, progress=80)
    agent_after_update = board.load(agent_goal.id)
    assert agent_after_update is not None
    agent_updated_ts = agent_after_update.updated

    # --- Step 2: write a user vault file with an OLDER updated timestamp ---
    # Use a timestamp that predates the agent's update.
    older_ts = "2020-01-01T00:00:00+00:00"
    goal_file = vault / "user" / "goals" / f"{agent_goal.id}.md"
    goal_file.write_text(
        f"---\ntype: goal\npriority: medium\ntitle: Ship Auth\n"
        f"updated: '{older_ts}'\nstatus: active\n---\n\n"
        "User edited description: simplified auth approach."
    )

    # --- Step 3: run _ingest_goal() ---
    meta = vs.scan("user/goals")[0]

    with caplog.at_level(logging.INFO, logger="obscura.kairos.vault_sync"):
        vs._ingest_goal(meta)

    # --- Step 4: in-memory version replaced with user's version ---
    board2 = GoalBoard(goals_dir=goals_dir)
    reloaded = board2.load(agent_goal.id)
    assert reloaded is not None, "Goal should still exist after ingest"
    # User file had priority=medium; agent had priority=high.
    assert reloaded.priority == "medium", (
        "User file priority (medium) should have replaced agent priority (high)"
    )
    # User file body should be present.
    assert "simplified auth approach" in (reloaded.body or ""), (
        "User file body should be the active version"
    )

    # INFO log should be present.
    conflict_logs = [
        r for r in caplog.records if "in-memory version replaced" in r.message
    ]
    assert conflict_logs, "Expected INFO log about in-memory version being replaced"

    # --- Step 5: old agent version was archived to .conflicts/ ---
    conflicts_dir = vault / "agent" / "goals" / ".conflicts"
    assert conflicts_dir.is_dir(), ".conflicts directory should have been created"
    conflict_files = list(conflicts_dir.glob(f"{agent_goal.id}.*.md"))
    assert len(conflict_files) == 1, (
        f"Expected exactly one conflict archive, got: {conflict_files}"
    )
    conflict_content = conflict_files[0].read_text(encoding="utf-8")
    # The archived version should capture the agent's progress (80%).
    assert "80" in conflict_content, (
        "Conflict archive should contain the agent's 80% progress"
    )
    # Sanity-check: archived updated timestamp matches agent's version.
    assert agent_updated_ts.replace(":", "") in conflict_content or any(
        part in conflict_content for part in agent_updated_ts.split("T")
    ), "Conflict archive should contain the agent's updated timestamp"


# ---------------------------------------------------------------------------
# _retry helper tests
# ---------------------------------------------------------------------------


def test_retry_succeeds_on_first_attempt() -> None:
    """_retry returns immediately when fn() succeeds on the first call."""
    import obscura.kairos.vault_sync as vs_mod
    from obscura.kairos.vault_sync import _retry

    slept: list[float] = []
    original_sleep = vs_mod._time.sleep
    vs_mod._time.sleep = slept.append
    try:
        result = _retry(lambda: 99, label="first_attempt")
        assert result == 99
        assert slept == [], "No sleep should occur on first-attempt success"
    finally:
        vs_mod._time.sleep = original_sleep


def test_retry_recovers_after_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fn() failing twice then succeeding on the 3rd attempt returns the result."""
    import obscura.kairos.vault_sync as vs_mod
    from obscura.kairos.vault_sync import _retry

    # Suppress actual sleeping.
    monkeypatch.setattr(vs_mod._time, "sleep", lambda _: None)

    call_log: list[int] = []

    def flaky() -> str:
        attempt = len(call_log) + 1
        call_log.append(attempt)
        if attempt < 3:  # noqa: PLR2004
            raise OSError("transient filesystem error")
        return "success"

    result = _retry(flaky, attempts=3, base_delay=0.5, label="queue_snapshot")
    assert result == "success"
    assert len(call_log) == 3  # noqa: PLR2004  — exactly 3 calls


def test_retry_logs_warning_and_reraises_after_all_attempts_fail(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After all attempts exhaust, a warning is logged and the exception is re-raised."""
    import logging

    import obscura.kairos.vault_sync as vs_mod
    from obscura.kairos.vault_sync import _retry

    monkeypatch.setattr(vs_mod._time, "sleep", lambda _: None)

    def always_fails() -> None:
        raise PermissionError("locked")

    with caplog.at_level(logging.WARNING, logger="obscura.kairos.vault_sync"):
        with pytest.raises(PermissionError, match="locked"):
            _retry(always_fails, attempts=3, label="export_goals")

    assert any(
        "All 3 attempts failed" in rec.message and "export_goals" in rec.message
        for rec in caplog.records
    ), f"Expected retry warning not found in: {[r.message for r in caplog.records]}"


def test_retry_debug_log_on_intermediate_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each failed attempt before the last emits a debug-level retry log."""
    import logging

    import obscura.kairos.vault_sync as vs_mod
    from obscura.kairos.vault_sync import _retry

    monkeypatch.setattr(vs_mod._time, "sleep", lambda _: None)

    attempt = 0

    def two_fails_then_ok() -> str:
        nonlocal attempt
        attempt += 1
        if attempt < 3:  # noqa: PLR2004
            raise OSError("nfs stall")
        return "done"

    with caplog.at_level(logging.DEBUG, logger="obscura.kairos.vault_sync"):
        result = _retry(two_fails_then_ok, attempts=3, label="export_queue_snapshot")

    assert result == "done"
    retry_logs = [r for r in caplog.records if "Retry" in r.message]
    assert len(retry_logs) == 2, (  # noqa: PLR2004  — attempts 1 and 2 each log
        f"Expected 2 retry debug messages, got: {[r.message for r in retry_logs]}"
    )


def test_export_queue_snapshot_retries_on_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transient write failure is retried; the 3rd attempt writes successfully."""
    import logging

    import obscura.kairos.vault_sync as vs_mod
    from obscura.kairos.vault_sync import VaultSync

    # Suppress sleeping in _retry.
    monkeypatch.setattr(vs_mod._time, "sleep", lambda _: None)

    vault = tmp_path / "vault"
    vs = VaultSync(vault_dir=vault)
    vs.bootstrap()

    snapshot_path = vault / "agent" / "tasks" / "queue-snapshot.md"

    # Stub out TaskQueue so it doesn't need a real DB.
    class FakeQueue:
        def queue_depth(self) -> dict:
            return {"50": 2}

    monkeypatch.setattr("obscura.core.task_queue.TaskQueue", FakeQueue)

    # Intercept Path.write_text on the snapshot path specifically:
    # fail twice, succeed on the 3rd call.
    write_calls: list[int] = []
    original_write_text = Path.write_text

    def patched_write_text(self: Path, content: str, **kwargs: object) -> None:
        if self == snapshot_path:
            write_calls.append(len(write_calls) + 1)
            if len(write_calls) < 3:  # noqa: PLR2004
                raise OSError("NFS write error (simulated)")
        return original_write_text(self, content, **kwargs)

    monkeypatch.setattr(Path, "write_text", patched_write_text)

    with caplog.at_level(logging.DEBUG, logger="obscura.kairos.vault_sync"):
        count = vs._export_all()

    # The queue snapshot eventually wrote (1 file exported).
    assert count >= 1
    assert snapshot_path.exists(), "Snapshot file should exist after retry success"
    assert len(write_calls) == 3  # noqa: PLR2004  — two failures + one success

    retry_msgs = [r.message for r in caplog.records if "Retry" in r.message]
    assert len(retry_msgs) >= 2, (  # noqa: PLR2004  — 2 retries before success
        f"Expected retry log messages, got: {retry_msgs}"
    )
