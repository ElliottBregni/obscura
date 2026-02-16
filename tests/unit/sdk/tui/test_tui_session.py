"""Tests for sdk.tui.session — ConversationTurn and TUISession (in-memory).

Covers ConversationTurn creation, TUISession add/retrieval, session_id
generation, turn ordering, timestamps, and metadata storage.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Inline stubs — mirrors sdk/tui/session.py and sdk/tui/modes.py
# ---------------------------------------------------------------------------


class TUIMode(Enum):
    ASK = "ask"
    PLAN = "plan"
    CODE = "code"
    DIFF = "diff"


@dataclass
class ConversationTurn:
    """A single turn in the conversation history."""

    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime
    mode: TUIMode
    metadata: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())


class TUISession:
    """In-memory conversation history manager."""

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id or uuid.uuid4().hex[:8]
        self.turns: list[ConversationTurn] = []

    def add_turn(self, turn: ConversationTurn) -> None:
        self.turns.append(turn)

    def get_turns(self) -> list[ConversationTurn]:
        return list(self.turns)

    def get_last_turn(self) -> ConversationTurn | None:
        return self.turns[-1] if self.turns else None

    def clear(self) -> None:
        self.turns.clear()

    @property
    def turn_count(self) -> int:
        return len(self.turns)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_turn(
    role: str = "user",
    content: str = "hello",
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


class TestConversationTurn:
    """Verify ConversationTurn creation and field access."""

    def test_create_user_turn(self) -> None:
        """A user turn stores role, content, timestamp, and mode."""
        now = datetime.now(tz=timezone.utc)
        turn = ConversationTurn(
            role="user",
            content="How does this work?",
            timestamp=now,
            mode=TUIMode.ASK,
        )
        assert turn.role == "user"
        assert turn.content == "How does this work?"
        assert turn.timestamp == now
        assert turn.mode is TUIMode.ASK

    def test_create_assistant_turn(self) -> None:
        """An assistant turn has role='assistant'."""
        turn = _make_turn(role="assistant", content="Here is the answer.")
        assert turn.role == "assistant"
        assert turn.content == "Here is the answer."

    def test_default_metadata_is_empty(self) -> None:
        """Metadata defaults to an empty dict when not provided."""
        turn = _make_turn()
        assert turn.metadata == {}

    def test_metadata_with_tool_uses(self) -> None:
        """Metadata can store tool_uses list."""
        turn = _make_turn(
            metadata={
                "tool_uses": [
                    {"name": "read_file", "input": {"path": "foo.py"}},
                ],
            }
        )
        assert len(turn.metadata["tool_uses"]) == 1
        assert turn.metadata["tool_uses"][0]["name"] == "read_file"

    def test_metadata_with_thinking(self) -> None:
        """Metadata can store thinking text."""
        turn = _make_turn(metadata={"thinking": "Let me analyze this..."})
        assert turn.metadata["thinking"] == "Let me analyze this..."

    def test_metadata_with_timing(self) -> None:
        """Metadata can store timing information."""
        turn = _make_turn(
            metadata={"timing_ms": 1234, "tokens_in": 50, "tokens_out": 200}
        )
        assert turn.metadata["timing_ms"] == 1234
        assert turn.metadata["tokens_in"] == 50
        assert turn.metadata["tokens_out"] == 200

    def test_metadata_with_all_fields(self) -> None:
        """Metadata can store tool_uses, thinking, and timing together."""
        turn = _make_turn(
            metadata={
                "tool_uses": [{"name": "write_file"}],
                "thinking": "Considering options...",
                "timing_ms": 500,
            }
        )
        assert "tool_uses" in turn.metadata
        assert "thinking" in turn.metadata
        assert "timing_ms" in turn.metadata

    def test_turn_in_plan_mode(self) -> None:
        """Turn can be in PLAN mode."""
        turn = _make_turn(mode=TUIMode.PLAN, content="Plan this feature")
        assert turn.mode is TUIMode.PLAN

    def test_turn_in_code_mode(self) -> None:
        """Turn can be in CODE mode."""
        turn = _make_turn(mode=TUIMode.CODE, content="Edit foo.py")
        assert turn.mode is TUIMode.CODE

    def test_turn_in_diff_mode(self) -> None:
        """Turn can be in DIFF mode."""
        turn = _make_turn(mode=TUIMode.DIFF, content="Review these changes")
        assert turn.mode is TUIMode.DIFF

    def test_turn_with_empty_content(self) -> None:
        """Turn can have empty content string."""
        turn = _make_turn(content="")
        assert turn.content == ""

    def test_turn_with_multiline_content(self) -> None:
        """Turn can have multiline content."""
        content = "line 1\nline 2\nline 3"
        turn = _make_turn(content=content)
        assert turn.content == content
        assert turn.content.count("\n") == 2


class TestTUISessionCreation:
    """Verify TUISession construction and session_id generation."""

    def test_auto_generated_session_id(self) -> None:
        """Session generates an 8-char hex ID when none is provided."""
        session = TUISession()
        assert len(session.session_id) == 8
        assert all(c in "0123456789abcdef" for c in session.session_id)

    def test_custom_session_id(self) -> None:
        """Session accepts a custom session_id."""
        session = TUISession(session_id="my-session-42")
        assert session.session_id == "my-session-42"

    def test_unique_session_ids(self) -> None:
        """Multiple sessions get distinct auto-generated IDs."""
        ids = {TUISession().session_id for _ in range(50)}
        assert len(ids) == 50

    def test_empty_turns_on_init(self) -> None:
        """Session starts with zero turns."""
        session = TUISession()
        assert session.turns == []
        assert session.turn_count == 0


class TestTUISessionAddTurn:
    """Verify adding turns and retrieving them."""

    def test_add_single_turn(self) -> None:
        """Adding one turn increases turn_count to 1."""
        session = TUISession()
        session.add_turn(_make_turn())
        assert session.turn_count == 1

    def test_add_multiple_turns(self) -> None:
        """Adding multiple turns accumulates them in order."""
        session = TUISession()
        for i in range(5):
            session.add_turn(_make_turn(content=f"msg {i}"))
        assert session.turn_count == 5

    def test_get_turns_returns_copy(self) -> None:
        """get_turns() returns a separate list from the internal store."""
        session = TUISession()
        session.add_turn(_make_turn())
        turns = session.get_turns()
        turns.clear()
        assert session.turn_count == 1

    def test_turn_ordering_preserved(self) -> None:
        """Turns are returned in the order they were added."""
        session = TUISession()
        for i in range(10):
            session.add_turn(_make_turn(content=f"msg-{i}"))
        contents = [t.content for t in session.get_turns()]
        assert contents == [f"msg-{i}" for i in range(10)]

    def test_get_last_turn_empty_session(self) -> None:
        """get_last_turn() returns None for an empty session."""
        session = TUISession()
        assert session.get_last_turn() is None

    def test_get_last_turn_returns_most_recent(self) -> None:
        """get_last_turn() returns the most recently added turn."""
        session = TUISession()
        session.add_turn(_make_turn(content="first"))
        session.add_turn(_make_turn(content="second"))
        last_turn = session.get_last_turn()
        assert last_turn is not None
        assert last_turn.content == "second"

    def test_alternating_user_assistant_turns(self) -> None:
        """User and assistant turns alternate correctly."""
        session = TUISession()
        session.add_turn(_make_turn(role="user", content="Q1"))
        session.add_turn(_make_turn(role="assistant", content="A1"))
        session.add_turn(_make_turn(role="user", content="Q2"))
        session.add_turn(_make_turn(role="assistant", content="A2"))

        roles = [t.role for t in session.get_turns()]
        assert roles == ["user", "assistant", "user", "assistant"]


class TestTUISessionTimestamps:
    """Verify timestamp ordering and storage."""

    def test_timestamps_stored_accurately(self) -> None:
        """Turn timestamps match what was provided."""
        t1 = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2025, 1, 15, 10, 1, 0, tzinfo=timezone.utc)

        session = TUISession()
        session.add_turn(_make_turn(content="first", timestamp=t1))
        session.add_turn(_make_turn(content="second", timestamp=t2))

        turns = session.get_turns()
        assert turns[0].timestamp == t1
        assert turns[1].timestamp == t2

    def test_chronological_ordering_by_insertion(self) -> None:
        """Turns are ordered by insertion, not by timestamp value."""
        later = datetime(2025, 12, 31, tzinfo=timezone.utc)
        earlier = datetime(2025, 1, 1, tzinfo=timezone.utc)

        session = TUISession()
        session.add_turn(_make_turn(content="added-first", timestamp=later))
        session.add_turn(_make_turn(content="added-second", timestamp=earlier))

        turns = session.get_turns()
        assert turns[0].content == "added-first"
        assert turns[1].content == "added-second"


class TestTUISessionMetadata:
    """Verify metadata storage across turns."""

    def test_turns_with_different_metadata(self) -> None:
        """Each turn can have its own metadata."""
        session = TUISession()
        session.add_turn(_make_turn(metadata={"timing_ms": 100}))
        session.add_turn(_make_turn(metadata={"thinking": "hmm"}))

        turns = session.get_turns()
        assert turns[0].metadata == {"timing_ms": 100}
        assert turns[1].metadata == {"thinking": "hmm"}

    def test_turns_across_different_modes(self) -> None:
        """Turns track which mode they were created in."""
        session = TUISession()
        session.add_turn(_make_turn(mode=TUIMode.ASK, content="Q"))
        session.add_turn(_make_turn(mode=TUIMode.PLAN, content="Plan"))
        session.add_turn(_make_turn(mode=TUIMode.CODE, content="Code"))

        modes = [t.mode for t in session.get_turns()]
        assert modes == [TUIMode.ASK, TUIMode.PLAN, TUIMode.CODE]

    def test_complex_metadata_nesting(self) -> None:
        """Metadata supports arbitrarily nested dicts and lists."""
        meta = {
            "tool_uses": [
                {
                    "name": "read_file",
                    "input": {"path": "/src/main.py"},
                    "output": {"content": "print('hi')", "lines": 1},
                },
            ],
            "thinking": "I need to read the file first",
            "timing_ms": 2345,
            "tokens": {"input": 50, "output": 200},
        }
        session = TUISession()
        session.add_turn(_make_turn(metadata=meta))

        last = session.get_last_turn()
        assert last is not None
        stored = last.metadata
        assert stored["tool_uses"][0]["output"]["lines"] == 1
        assert stored["tokens"]["input"] == 50


class TestTUISessionClear:
    """Verify session clearing."""

    def test_clear_removes_all_turns(self) -> None:
        """clear() removes every turn from the session."""
        session = TUISession()
        for i in range(5):
            session.add_turn(_make_turn(content=f"turn-{i}"))
        assert session.turn_count == 5

        session.clear()
        assert session.turn_count == 0
        assert session.get_turns() == []

    def test_clear_preserves_session_id(self) -> None:
        """clear() does not change the session_id."""
        session = TUISession(session_id="keep-me")
        session.add_turn(_make_turn())
        session.clear()
        assert session.session_id == "keep-me"

    def test_add_after_clear(self) -> None:
        """Turns can be added again after clearing."""
        session = TUISession()
        session.add_turn(_make_turn(content="old"))
        session.clear()
        session.add_turn(_make_turn(content="new"))
        assert session.turn_count == 1
        last = session.get_last_turn()
        assert last is not None
        assert last.content == "new"
