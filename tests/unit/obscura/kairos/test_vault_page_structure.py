"""Tests for vault export page structure.

Covers:
  - VaultSync._render_page produces consistent frontmatter ordering and
    distinguishes entity vs snapshot timestamp conventions.
  - Goal pages from the GoalBoard fallback path render linked tasks
    as a body section.
  - Queue snapshot includes goal-page backlinks for top pending tasks.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from obscura.kairos.vault_sync import VaultSync


# ---------------------------------------------------------------------------
# _render_page contract
# ---------------------------------------------------------------------------


class TestRenderPage:
    def test_entity_page_uses_created_updated(self) -> None:
        page = VaultSync._render_page(
            page_type="goal",
            page_id="g1",
            body="hello",
            created_at="2026-01-01T00:00:00",
            updated_at="2026-02-01T00:00:00",
            extra_fields={"title": "G1", "status": "active"},
        )
        body, fm = _split(page)
        assert fm["id"] == "g1"
        assert fm["type"] == "goal"
        assert fm["created_at"] == "2026-01-01T00:00:00"
        assert fm["updated_at"] == "2026-02-01T00:00:00"
        assert "generated_at" not in fm
        assert fm["title"] == "G1"
        assert body.strip() == "hello"

    def test_snapshot_page_uses_generated_at_only(self) -> None:
        page = VaultSync._render_page(
            page_type="queue_snapshot",
            page_id="queue-snapshot",
            body="snap body",
            generated_at="2026-03-01T00:00:00",
            extra_fields={"total_pending": 5},
        )
        _, fm = _split(page)
        assert fm["generated_at"] == "2026-03-01T00:00:00"
        assert "created_at" not in fm
        assert "updated_at" not in fm
        assert fm["total_pending"] == 5

    def test_frontmatter_ordering_is_stable(self) -> None:
        """id and type lead, then timestamps, then extras in dict order.
        Same input → byte-identical output (no spurious diffs across
        sync ticks)."""
        kwargs = {
            "page_type": "goal",
            "page_id": "g1",
            "body": "b",
            "created_at": "2026-01-01T00:00:00",
            "extra_fields": {"title": "T", "status": "active"},
        }
        a = VaultSync._render_page(**kwargs)
        b = VaultSync._render_page(**kwargs)
        assert a == b
        # id, type, created_at, then extras — all on consecutive lines.
        lines = [
            ln.split(":", 1)[0]
            for ln in a.splitlines()
            if ln and not ln.startswith("---") and not ln.startswith(" ")
        ]
        # First four meaningful lines: id, type, created_at, title.
        assert lines[:4] == ["id", "type", "created_at", "title"]

    def test_extras_cannot_override_canonical_fields(self) -> None:
        """If a caller mistakenly puts ``id`` or ``type`` in extras, the
        canonical values still win — extras are appended, not merged."""
        page = VaultSync._render_page(
            page_type="goal",
            page_id="g1",
            body="b",
            extra_fields={"id": "wrong", "type": "wrong", "title": "T"},
        )
        _, fm = _split(page)
        assert fm["id"] == "g1"
        assert fm["type"] == "goal"


# ---------------------------------------------------------------------------
# Goal export — body section + frontmatter
# ---------------------------------------------------------------------------


class TestGoalExportStructure:
    def test_goalboard_fallback_renders_linked_tasks_section(
        self, tmp_path: Path, monkeypatch: object
    ) -> None:
        """When a goal has linked tasks, the export body contains a
        '## Linked tasks' section listing the task IDs."""
        sync = VaultSync(vault_dir=tmp_path)

        existing_goal = SimpleNamespace(
            id="g1",
            title="G1",
            status="active",
            priority="high",
            progress=0,
            created="2026-01-01T00:00:00",
            updated="2026-01-02T00:00:00",
            acceptance_criteria=("AC1",),
            tasks=("t-aaa", "t-bbb"),
            body="Original body text.",
        )

        class _FakeBoard:
            def load_all(self) -> list[SimpleNamespace]:
                return [existing_goal]

        # KAIROS source disabled by patching create_goal_store to fail.
        with (
            patch("obscura.kairos.vault_sync.GoalBoard", return_value=_FakeBoard()),
            patch(
                "obscura.kairos.vault_sync.create_goal_store",
                side_effect=RuntimeError("no kairos.db in test"),
            ),
        ):
            written = sync._export_goals()  # pyright: ignore[reportPrivateUsage]

        assert written == 1
        page_path = tmp_path / "agent" / "goals" / "g1.md"
        assert page_path.exists()
        content = page_path.read_text()
        assert "## Linked tasks" in content
        assert "- t-aaa" in content
        assert "- t-bbb" in content
        # Frontmatter still present and correct.
        _body, fm = _split(content)
        assert fm["id"] == "g1"
        assert fm["type"] == "goal"
        assert fm["status"] == "active"
        assert fm["created_at"] == "2026-01-01T00:00:00"
        assert fm["updated_at"] == "2026-01-02T00:00:00"

    def test_stale_goal_pages_swept_after_writes(
        self, tmp_path: Path
    ) -> None:
        """A goal page that was exported in a prior run but is no longer
        in either source should be removed AFTER the live set is written
        (so any in-flight reader sees a consistent state)."""
        sync = VaultSync(vault_dir=tmp_path)
        goals_dir = tmp_path / "agent" / "goals"
        goals_dir.mkdir(parents=True, exist_ok=True)
        # Pre-existing stale page.
        stale = goals_dir / "old-goal.md"
        stale.write_text("---\nid: old-goal\n---\n")
        # Live goal that should remain.
        live_goal = SimpleNamespace(
            id="live", title="Live", status="active", priority="medium",
            progress=0, created="", updated="", acceptance_criteria=(),
            tasks=(), body="",
        )

        class _FakeBoard:
            def load_all(self) -> list[SimpleNamespace]:
                return [live_goal]

        with (
            patch("obscura.kairos.vault_sync.GoalBoard", return_value=_FakeBoard()),
            patch(
                "obscura.kairos.vault_sync.create_goal_store",
                side_effect=RuntimeError("no kairos.db"),
            ),
        ):
            sync._export_goals()  # pyright: ignore[reportPrivateUsage]

        assert (goals_dir / "live.md").exists()
        assert not stale.exists()


# ---------------------------------------------------------------------------
# Queue snapshot — backlinks to goal pages
# ---------------------------------------------------------------------------


class TestQueueSnapshotBacklinks:
    def test_top_tasks_backlink_to_their_goal_page(self, tmp_path: Path) -> None:
        """Pending tasks with a goal_id render with a backlink to their
        goal page; tasks without one are listed plain."""
        sync = VaultSync(vault_dir=tmp_path)

        class _FakeQueue:
            def queue_depth(self) -> dict[str, int]:
                return {"50": 2}

        fake_tasks = [
            {"task_id": "t1", "subject": "Fix login", "goal_id": "g-auth", "priority": 50},
            {"task_id": "t2", "subject": "Write tests", "goal_id": "", "priority": 50},
        ]

        with (
            patch("obscura.kairos.vault_sync.TaskQueue", return_value=_FakeQueue()),
            patch.object(VaultSync, "_top_pending_tasks", return_value=fake_tasks),
        ):
            written = sync._export_queue_snapshot()  # pyright: ignore[reportPrivateUsage]

        assert written == 1
        snapshot = (tmp_path / "agent" / "tasks" / "queue-snapshot.md").read_text()
        # Task with a goal_id has a backlink.
        assert "[[../goals/g-auth]]" in snapshot
        assert "Fix login" in snapshot
        # Task without a goal_id does NOT have a backlink (no goal page exists).
        assert "Write tests" in snapshot
        # Specifically: no `→ [[` arrow on that line.
        for line in snapshot.splitlines():
            if "Write tests" in line:
                assert "→" not in line


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split(content: str) -> tuple[str, dict[str, object]]:
    """Split a rendered page into (body, parsed_frontmatter)."""
    assert content.startswith("---\n"), content[:40]
    rest = content[4:]
    end = rest.index("---")
    fm_text = rest[:end]
    body = rest[end + 4 :]  # skip "---\n"
    fm = yaml.safe_load(fm_text) or {}
    return body, fm
