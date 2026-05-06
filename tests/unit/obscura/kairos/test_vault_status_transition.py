"""Regression tests for the vault-sync illegal-status-transition warning.

Symptom we're guarding against: every sync tick, vault_sync._ingest_goal
unconditionally applied the markdown frontmatter's `status` field via
GoalBoard.update, including illegal transitions like
``completed → in_progress``. The FSM rejected each one and logged a
WARNING. The user observed:

    Invalid transition completed → in_progress for goal build-arbiter-judge
    Invalid transition completed → in_progress for goal ship-task-queue-system

The fix: filter status at the producer (vault sync) so illegal
transitions never reach the FSM. Skip the status field, info-log once
per goal, apply the rest of the frontmatter normally.
"""

from __future__ import annotations

import logging

import pytest

from obscura.kairos.goals import is_valid_status_transition


# ---------------------------------------------------------------------------
# is_valid_status_transition
# ---------------------------------------------------------------------------


class TestIsValidStatusTransition:
    @pytest.mark.parametrize(
        ("current", "new"),
        [
            ("draft", "active"),
            ("draft", "abandoned"),
            ("active", "in_progress"),
            ("active", "completed"),
            ("active", "abandoned"),
            ("in_progress", "completed"),
            ("in_progress", "abandoned"),
            ("in_progress", "active"),
            ("abandoned", "active"),
        ],
    )
    def test_legal_transitions(self, current: str, new: str) -> None:
        assert is_valid_status_transition(current, new) is True

    @pytest.mark.parametrize(
        ("current", "new"),
        [
            # The actual bug we're fixing.
            ("completed", "in_progress"),
            ("completed", "active"),
            ("completed", "draft"),
            ("completed", "abandoned"),
            # Other regressions the FSM forbids.
            ("draft", "in_progress"),
            ("draft", "completed"),
            ("abandoned", "in_progress"),
            ("abandoned", "completed"),
        ],
    )
    def test_illegal_transitions(self, current: str, new: str) -> None:
        assert is_valid_status_transition(current, new) is False

    def test_same_status_is_noop_and_legal(self) -> None:
        """Setting status to its current value is a no-op write; we treat
        it as legal so the producer can blindly call update without
        special-casing."""
        for state in ("draft", "active", "in_progress", "completed", "abandoned"):
            assert is_valid_status_transition(state, state) is True

    def test_unknown_current_status_blocks_anything(self) -> None:
        """An unrecognized current status doesn't appear in _TRANSITIONS;
        the helper must default to 'no transitions allowed' rather than
        silently allow."""
        assert is_valid_status_transition("garbage", "active") is False


# ---------------------------------------------------------------------------
# Vault sync illegal-transition handling
# ---------------------------------------------------------------------------


class TestVaultSyncIllegalTransition:
    def test_skips_illegal_transition_and_info_logs_once(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Simulate the symptom: existing goal is `completed`, vault file
        says `in_progress`. _ingest_goal should drop the status field
        from the update, log INFO once, and on subsequent ticks log
        nothing further for the same (goal, requested-status) pair."""
        from types import SimpleNamespace
        from unittest.mock import patch

        from obscura.kairos.vault_sync import VaultSync

        sync = VaultSync()

        existing_goal = SimpleNamespace(
            id="build-arbiter-judge",
            status="completed",
            updated="",  # empty so we don't trip _archive_conflict
            title="Build Arbiter Judge",
            priority="medium",
            acceptance_criteria=(),
            tasks=(),
        )

        captured_updates: list[dict[str, object]] = []

        class _FakeBoard:
            def load(self, _goal_id: str) -> SimpleNamespace:
                return existing_goal

            def get_if_newer(self, _goal_id: str, *, since: str) -> None:
                _ = since
                return None

            def update(self, _goal_id: str, **fields: object) -> SimpleNamespace:
                captured_updates.append(fields)
                return existing_goal

        meta = SimpleNamespace(
            path=SimpleNamespace(stem="build-arbiter-judge"),
            frontmatter={
                "title": "Build Arbiter Judge",
                "status": "in_progress",  # illegal: completed → in_progress
                "priority": "high",
                "acceptance_criteria": ["AC1", "AC2"],
                "updated": "2025-12-15T00:00:00",
            },
            body="updated body",
        )

        with (
            patch(
                "obscura.kairos.vault_sync.GoalBoard",
                return_value=_FakeBoard(),
            ),
            caplog.at_level(logging.INFO),
        ):
            sync._ingest_goal(meta)  # pyright: ignore[reportPrivateUsage]
            sync._ingest_goal(meta)  # second tick — same illegal transition  # pyright: ignore[reportPrivateUsage]
            sync._ingest_goal(meta)  # third tick  # pyright: ignore[reportPrivateUsage]

        # Status was filtered out of every update call.
        assert len(captured_updates) == 3
        for fields in captured_updates:
            assert "status" not in fields
            # Other fields still went through.
            assert fields.get("priority") == "high"
            assert fields.get("acceptance_criteria") == ["AC1", "AC2"]
            assert fields.get("body") == "updated body"

        # And the "skipping" notice fired exactly once across three ticks.
        skipping_logs = [
            rec
            for rec in caplog.records
            if "Skipping illegal status transition" in rec.message
        ]
        assert len(skipping_logs) == 1
        assert skipping_logs[0].levelname == "INFO"

    def test_legal_transition_passes_through(self) -> None:
        """When the requested status IS a legal transition, status field
        is preserved and applied normally."""
        from types import SimpleNamespace
        from unittest.mock import patch

        from obscura.kairos.vault_sync import VaultSync

        sync = VaultSync()
        existing_goal = SimpleNamespace(
            id="g1",
            status="active",  # active → in_progress is legal
            updated="",  # avoid _archive_conflict path; tested elsewhere
            title="G1",
            priority="medium",
            acceptance_criteria=(),
            tasks=(),
        )
        captured: list[dict[str, object]] = []

        class _FakeBoard:
            def load(self, _goal_id: str) -> SimpleNamespace:
                return existing_goal

            def get_if_newer(self, _goal_id: str, *, since: str) -> None:
                _ = since
                return None

            def update(self, _goal_id: str, **fields: object) -> SimpleNamespace:
                captured.append(fields)
                return existing_goal

        meta = SimpleNamespace(
            path=SimpleNamespace(stem="g1"),
            frontmatter={"status": "in_progress"},
            body="",
        )

        with patch(
            "obscura.kairos.vault_sync.GoalBoard",
            return_value=_FakeBoard(),
        ):
            sync._ingest_goal(meta)  # pyright: ignore[reportPrivateUsage]

        assert len(captured) == 1
        assert captured[0].get("status") == "in_progress"
