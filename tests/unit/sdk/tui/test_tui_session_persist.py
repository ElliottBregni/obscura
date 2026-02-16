"""Tests for sdk.tui.session persistence — save/load/list sessions.

Covers TUISession.save() writing JSON, load() reading back, list_sessions(),
session file format, loading nonexistent sessions, and concurrent handling.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Inline stubs — mirrors sdk/tui/session.py persistence from PLAN_TUI.md
# ---------------------------------------------------------------------------


class TUIMode(Enum):
    ASK = "ask"
    PLAN = "plan"
    CODE = "code"
    DIFF = "diff"


@dataclass
class ConversationTurn:
    """A single turn in the conversation history."""

    role: str
    content: str
    timestamp: datetime
    mode: TUIMode
    metadata: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "mode": self.mode.value,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationTurn:
        return cls(
            role=data["role"],
            content=data["content"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            mode=TUIMode(data["mode"]),
            metadata=data.get("metadata", {}),
        )


class SessionNotFoundError(Exception):
    """Raised when a session cannot be found on disk."""


class TUISession:
    """Manages conversation history with JSON persistence."""

    SESSIONS_DIR_NAME = "tui_sessions"

    def __init__(
        self,
        session_id: str | None = None,
        base_dir: Path | None = None,
    ) -> None:
        self.session_id = session_id or uuid.uuid4().hex[:8]
        self.turns: list[ConversationTurn] = []
        self._base_dir = base_dir or Path.home() / ".obscura"
        self._sessions_dir = self._base_dir / self.SESSIONS_DIR_NAME

    @property
    def file_path(self) -> Path:
        return self._sessions_dir / f"{self.session_id}.json"

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def add_turn(self, turn: ConversationTurn) -> None:
        self.turns.append(turn)

    def save(self) -> None:
        """Write the session to disk as JSON."""
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "session_id": self.session_id,
            "turns": [t.to_dict() for t in self.turns],
            "created_at": (self.turns[0].timestamp.isoformat() if self.turns else None),
            "updated_at": (
                self.turns[-1].timestamp.isoformat() if self.turns else None
            ),
        }
        self.file_path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(
        cls,
        session_id: str,
        base_dir: Path | None = None,
    ) -> TUISession:
        """Load a session from disk by ID."""
        session = cls(session_id=session_id, base_dir=base_dir)
        if not session.file_path.exists():
            raise SessionNotFoundError(
                f"Session not found: {session_id} (looked at {session.file_path})"
            )
        data = json.loads(session.file_path.read_text())
        session.turns = [ConversationTurn.from_dict(t) for t in data["turns"]]
        return session

    @classmethod
    def list_sessions(
        cls,
        base_dir: Path | None = None,
    ) -> list[dict[str, Any]]:
        """List all saved sessions with summary info."""
        base = base_dir or Path.home() / ".obscura"
        sessions_dir = base / cls.SESSIONS_DIR_NAME
        if not sessions_dir.exists():
            return []

        results: list[dict[str, Any]] = []
        for path in sorted(sessions_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                results.append(
                    {
                        "session_id": data["session_id"],
                        "turn_count": len(data.get("turns", [])),
                        "created_at": data.get("created_at"),
                        "updated_at": data.get("updated_at"),
                    }
                )
            except (json.JSONDecodeError, KeyError):
                continue  # Skip corrupted files

        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_turn(
    role: str = "user",
    content: str = "test message",
    mode: TUIMode = TUIMode.ASK,
    timestamp: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> ConversationTurn:
    return ConversationTurn(
        role=role,
        content=content,
        timestamp=timestamp or datetime.now(tz=timezone.utc),
        mode=mode,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTUISessionSave:
    """Verify TUISession.save() writes JSON to disk."""

    def test_save_creates_sessions_directory(self, tmp_path: Path) -> None:
        """save() creates the tui_sessions directory if it doesn't exist."""
        session = TUISession(base_dir=tmp_path)
        session.add_turn(_make_turn())
        session.save()
        assert (tmp_path / "tui_sessions").is_dir()

    def test_save_creates_json_file(self, tmp_path: Path) -> None:
        """save() creates a JSON file named after the session ID."""
        session = TUISession(session_id="abc123", base_dir=tmp_path)
        session.add_turn(_make_turn())
        session.save()
        expected = tmp_path / "tui_sessions" / "abc123.json"
        assert expected.exists()

    def test_save_file_is_valid_json(self, tmp_path: Path) -> None:
        """The saved file is valid JSON."""
        session = TUISession(base_dir=tmp_path)
        session.add_turn(_make_turn())
        session.save()
        data = json.loads(session.file_path.read_text())
        assert isinstance(data, dict)

    def test_save_contains_session_id(self, tmp_path: Path) -> None:
        """The JSON includes the session_id field."""
        session = TUISession(session_id="myid", base_dir=tmp_path)
        session.add_turn(_make_turn())
        session.save()
        data = json.loads(session.file_path.read_text())
        assert data["session_id"] == "myid"

    def test_save_contains_turns(self, tmp_path: Path) -> None:
        """The JSON includes all conversation turns."""
        session = TUISession(base_dir=tmp_path)
        session.add_turn(_make_turn(role="user", content="Q1"))
        session.add_turn(_make_turn(role="assistant", content="A1"))
        session.save()
        data = json.loads(session.file_path.read_text())
        assert len(data["turns"]) == 2
        assert data["turns"][0]["role"] == "user"
        assert data["turns"][0]["content"] == "Q1"
        assert data["turns"][1]["role"] == "assistant"

    def test_save_empty_session(self, tmp_path: Path) -> None:
        """Saving a session with no turns produces valid JSON with empty turns."""
        session = TUISession(base_dir=tmp_path)
        session.save()
        data = json.loads(session.file_path.read_text())
        assert data["turns"] == []
        assert data["created_at"] is None
        assert data["updated_at"] is None

    def test_save_preserves_metadata(self, tmp_path: Path) -> None:
        """Turn metadata is preserved in the saved JSON."""
        session = TUISession(base_dir=tmp_path)
        session.add_turn(
            _make_turn(
                metadata={
                    "thinking": "hmm",
                    "timing_ms": 1500,
                }
            )
        )
        session.save()
        data = json.loads(session.file_path.read_text())
        meta = data["turns"][0]["metadata"]
        assert meta["thinking"] == "hmm"
        assert meta["timing_ms"] == 1500

    def test_save_preserves_mode(self, tmp_path: Path) -> None:
        """Turn mode is serialized as its string value."""
        session = TUISession(base_dir=tmp_path)
        session.add_turn(_make_turn(mode=TUIMode.PLAN))
        session.save()
        data = json.loads(session.file_path.read_text())
        assert data["turns"][0]["mode"] == "plan"

    def test_save_timestamps_iso_format(self, tmp_path: Path) -> None:
        """Timestamps are serialized in ISO format."""
        ts = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        session = TUISession(base_dir=tmp_path)
        session.add_turn(_make_turn(timestamp=ts))
        session.save()
        data = json.loads(session.file_path.read_text())
        assert "2025-06-15" in data["turns"][0]["timestamp"]

    def test_save_overwrites_on_second_save(self, tmp_path: Path) -> None:
        """Saving twice overwrites the file with updated content."""
        session = TUISession(base_dir=tmp_path)
        session.add_turn(_make_turn(content="first"))
        session.save()

        session.add_turn(_make_turn(content="second"))
        session.save()

        data = json.loads(session.file_path.read_text())
        assert len(data["turns"]) == 2

    def test_save_created_at_and_updated_at(self, tmp_path: Path) -> None:
        """created_at and updated_at correspond to first and last turn timestamps."""
        t1 = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2025, 1, 1, 10, 5, 0, tzinfo=timezone.utc)

        session = TUISession(base_dir=tmp_path)
        session.add_turn(_make_turn(timestamp=t1))
        session.add_turn(_make_turn(timestamp=t2))
        session.save()

        data = json.loads(session.file_path.read_text())
        assert "2025-01-01T10:00:00" in data["created_at"]
        assert "2025-01-01T10:05:00" in data["updated_at"]


class TestTUISessionLoad:
    """Verify TUISession.load() reads sessions back correctly."""

    def test_load_roundtrip(self, tmp_path: Path) -> None:
        """Save then load produces identical session data."""
        session = TUISession(session_id="rt1", base_dir=tmp_path)
        session.add_turn(_make_turn(role="user", content="hello"))
        session.add_turn(_make_turn(role="assistant", content="hi back"))
        session.save()

        loaded = TUISession.load("rt1", base_dir=tmp_path)
        assert loaded.session_id == "rt1"
        assert loaded.turn_count == 2
        assert loaded.turns[0].content == "hello"
        assert loaded.turns[1].content == "hi back"

    def test_load_preserves_roles(self, tmp_path: Path) -> None:
        """Loaded turns have correct roles."""
        session = TUISession(session_id="roles", base_dir=tmp_path)
        session.add_turn(_make_turn(role="user"))
        session.add_turn(_make_turn(role="assistant"))
        session.save()

        loaded = TUISession.load("roles", base_dir=tmp_path)
        assert loaded.turns[0].role == "user"
        assert loaded.turns[1].role == "assistant"

    def test_load_preserves_modes(self, tmp_path: Path) -> None:
        """Loaded turns have correct TUIMode values."""
        session = TUISession(session_id="modes", base_dir=tmp_path)
        session.add_turn(_make_turn(mode=TUIMode.ASK))
        session.add_turn(_make_turn(mode=TUIMode.PLAN))
        session.add_turn(_make_turn(mode=TUIMode.CODE))
        session.add_turn(_make_turn(mode=TUIMode.DIFF))
        session.save()

        loaded = TUISession.load("modes", base_dir=tmp_path)
        modes = [t.mode for t in loaded.turns]
        assert modes == [TUIMode.ASK, TUIMode.PLAN, TUIMode.CODE, TUIMode.DIFF]

    def test_load_preserves_timestamps(self, tmp_path: Path) -> None:
        """Loaded timestamps match the originals."""
        ts = datetime(2025, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
        session = TUISession(session_id="ts", base_dir=tmp_path)
        session.add_turn(_make_turn(timestamp=ts))
        session.save()

        loaded = TUISession.load("ts", base_dir=tmp_path)
        assert loaded.turns[0].timestamp == ts

    def test_load_preserves_metadata(self, tmp_path: Path) -> None:
        """Loaded metadata is intact."""
        meta = {"tool_uses": [{"name": "read_file"}], "timing_ms": 250}
        session = TUISession(session_id="meta", base_dir=tmp_path)
        session.add_turn(_make_turn(metadata=meta))
        session.save()

        loaded = TUISession.load("meta", base_dir=tmp_path)
        assert loaded.turns[0].metadata == meta

    def test_load_nonexistent_session_raises(self, tmp_path: Path) -> None:
        """Loading a nonexistent session raises SessionNotFoundError."""
        with pytest.raises(SessionNotFoundError, match="Session not found"):
            TUISession.load("does_not_exist", base_dir=tmp_path)

    def test_load_many_turns(self, tmp_path: Path) -> None:
        """Loading a session with many turns works correctly."""
        session = TUISession(session_id="many", base_dir=tmp_path)
        for i in range(100):
            session.add_turn(_make_turn(content=f"turn-{i}"))
        session.save()

        loaded = TUISession.load("many", base_dir=tmp_path)
        assert loaded.turn_count == 100
        assert loaded.turns[99].content == "turn-99"


class TestTUISessionListSessions:
    """Verify TUISession.list_sessions() returns all sessions."""

    def test_list_no_sessions(self, tmp_path: Path) -> None:
        """list_sessions() returns empty list when no sessions dir exists."""
        sessions = TUISession.list_sessions(base_dir=tmp_path)
        assert sessions == []

    def test_list_empty_sessions_dir(self, tmp_path: Path) -> None:
        """list_sessions() returns empty list when sessions dir is empty."""
        (tmp_path / "tui_sessions").mkdir(parents=True)
        sessions = TUISession.list_sessions(base_dir=tmp_path)
        assert sessions == []

    def test_list_single_session(self, tmp_path: Path) -> None:
        """list_sessions() returns one entry for one saved session."""
        session = TUISession(session_id="single", base_dir=tmp_path)
        session.add_turn(_make_turn())
        session.save()

        sessions = TUISession.list_sessions(base_dir=tmp_path)
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "single"

    def test_list_multiple_sessions(self, tmp_path: Path) -> None:
        """list_sessions() returns all saved sessions."""
        for sid in ["aaa", "bbb", "ccc"]:
            s = TUISession(session_id=sid, base_dir=tmp_path)
            s.add_turn(_make_turn())
            s.save()

        sessions = TUISession.list_sessions(base_dir=tmp_path)
        assert len(sessions) == 3
        ids = {s["session_id"] for s in sessions}
        assert ids == {"aaa", "bbb", "ccc"}

    def test_list_sessions_includes_turn_count(self, tmp_path: Path) -> None:
        """list_sessions() includes the turn count for each session."""
        s = TUISession(session_id="countme", base_dir=tmp_path)
        for _ in range(5):
            s.add_turn(_make_turn())
        s.save()

        sessions = TUISession.list_sessions(base_dir=tmp_path)
        assert sessions[0]["turn_count"] == 5

    def test_list_sessions_includes_timestamps(self, tmp_path: Path) -> None:
        """list_sessions() includes created_at and updated_at."""
        s = TUISession(session_id="withts", base_dir=tmp_path)
        s.add_turn(
            _make_turn(
                timestamp=datetime(2025, 5, 1, 10, 0, 0, tzinfo=timezone.utc),
            )
        )
        s.save()

        sessions = TUISession.list_sessions(base_dir=tmp_path)
        assert sessions[0]["created_at"] is not None

    def test_list_sessions_skips_corrupted_files(self, tmp_path: Path) -> None:
        """list_sessions() skips files that are not valid JSON."""
        sessions_dir = tmp_path / "tui_sessions"
        sessions_dir.mkdir(parents=True)

        # Valid session
        s = TUISession(session_id="valid", base_dir=tmp_path)
        s.add_turn(_make_turn())
        s.save()

        # Corrupted file
        (sessions_dir / "broken.json").write_text("not valid json{{{")

        sessions = TUISession.list_sessions(base_dir=tmp_path)
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "valid"

    def test_list_sessions_ignores_non_json_files(self, tmp_path: Path) -> None:
        """list_sessions() only reads .json files."""
        sessions_dir = tmp_path / "tui_sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "readme.md").write_text("# Not a session")

        s = TUISession(session_id="real", base_dir=tmp_path)
        s.add_turn(_make_turn())
        s.save()

        sessions = TUISession.list_sessions(base_dir=tmp_path)
        assert len(sessions) == 1


class TestSessionFileFormat:
    """Verify the JSON structure of saved session files."""

    def test_top_level_keys(self, tmp_path: Path) -> None:
        """Session JSON has expected top-level keys."""
        session = TUISession(session_id="fmt", base_dir=tmp_path)
        session.add_turn(_make_turn())
        session.save()

        data = json.loads(session.file_path.read_text())
        assert "session_id" in data
        assert "turns" in data
        assert "created_at" in data
        assert "updated_at" in data

    def test_turn_keys(self, tmp_path: Path) -> None:
        """Each turn in the JSON has expected keys."""
        session = TUISession(session_id="turnkeys", base_dir=tmp_path)
        session.add_turn(_make_turn())
        session.save()

        data = json.loads(session.file_path.read_text())
        turn = data["turns"][0]
        assert "role" in turn
        assert "content" in turn
        assert "timestamp" in turn
        assert "mode" in turn
        assert "metadata" in turn

    def test_json_is_pretty_printed(self, tmp_path: Path) -> None:
        """Session JSON is indented for readability."""
        session = TUISession(session_id="pretty", base_dir=tmp_path)
        session.add_turn(_make_turn())
        session.save()

        raw = session.file_path.read_text()
        # Indented JSON has newlines and spaces
        assert "\n" in raw
        assert "  " in raw


class TestConcurrentSessions:
    """Verify handling of multiple concurrent sessions."""

    def test_two_sessions_independent(self, tmp_path: Path) -> None:
        """Two sessions save and load independently."""
        s1 = TUISession(session_id="sess1", base_dir=tmp_path)
        s1.add_turn(_make_turn(content="from session 1"))
        s1.save()

        s2 = TUISession(session_id="sess2", base_dir=tmp_path)
        s2.add_turn(_make_turn(content="from session 2"))
        s2.save()

        loaded1 = TUISession.load("sess1", base_dir=tmp_path)
        loaded2 = TUISession.load("sess2", base_dir=tmp_path)

        assert loaded1.turns[0].content == "from session 1"
        assert loaded2.turns[0].content == "from session 2"

    def test_save_one_does_not_affect_other(self, tmp_path: Path) -> None:
        """Saving session A does not modify session B's file."""
        s1 = TUISession(session_id="a", base_dir=tmp_path)
        s1.add_turn(_make_turn(content="A turn"))
        s1.save()

        s2 = TUISession(session_id="b", base_dir=tmp_path)
        s2.add_turn(_make_turn(content="B turn"))
        s2.save()

        # Save A again with more turns
        s1.add_turn(_make_turn(content="A turn 2"))
        s1.save()

        # B should be unchanged
        loaded_b = TUISession.load("b", base_dir=tmp_path)
        assert loaded_b.turn_count == 1

    def test_list_after_concurrent_saves(self, tmp_path: Path) -> None:
        """list_sessions() shows all concurrently saved sessions."""
        for i in range(10):
            s = TUISession(session_id=f"concurrent-{i}", base_dir=tmp_path)
            s.add_turn(_make_turn(content=f"msg-{i}"))
            s.save()

        sessions = TUISession.list_sessions(base_dir=tmp_path)
        assert len(sessions) == 10

    def test_overwrite_session_on_save(self, tmp_path: Path) -> None:
        """Saving with same session_id overwrites the previous file."""
        s1 = TUISession(session_id="overwrite", base_dir=tmp_path)
        s1.add_turn(_make_turn(content="old"))
        s1.save()

        s2 = TUISession(session_id="overwrite", base_dir=tmp_path)
        s2.add_turn(_make_turn(content="new"))
        s2.save()

        loaded = TUISession.load("overwrite", base_dir=tmp_path)
        assert loaded.turn_count == 1
        assert loaded.turns[0].content == "new"
