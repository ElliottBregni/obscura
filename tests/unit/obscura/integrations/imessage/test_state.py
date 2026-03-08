"""Tests for IMessageState -- persistent last-seen ROWID tracking."""

from __future__ import annotations

from pathlib import Path

import pytest

from obscura.integrations.imessage.state import IMessageState


class TestIMessageStateInit:
    def test_initial_state_is_zero(self, tmp_path: Path) -> None:
        state = IMessageState(state_path=tmp_path / "state.json")
        assert state.last_rowid == 0

    def test_loads_existing_state(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text('{"last_rowid": 42}')
        state = IMessageState(state_path=path)
        assert state.last_rowid == 42

    def test_handles_corrupt_json(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("not json")
        state = IMessageState(state_path=path)
        assert state.last_rowid == 0


class TestIMessageStateUpdate:
    def test_update_persists(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        state = IMessageState(state_path=path)
        state.update(42)
        assert state.last_rowid == 42
        # Reload from disk
        state2 = IMessageState(state_path=path)
        assert state2.last_rowid == 42

    def test_update_only_advances(self, tmp_path: Path) -> None:
        state = IMessageState(state_path=tmp_path / "state.json")
        state.update(100)
        state.update(50)  # should not regress
        assert state.last_rowid == 100

    def test_update_zero_is_noop(self, tmp_path: Path) -> None:
        state = IMessageState(state_path=tmp_path / "state.json")
        state.update(10)
        state.update(0)
        assert state.last_rowid == 10

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "state.json"
        state = IMessageState(state_path=path)
        state.update(5)
        assert path.exists()
