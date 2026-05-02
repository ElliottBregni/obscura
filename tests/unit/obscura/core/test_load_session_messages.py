"""``load_session_messages`` reconstructs Message objects from event log.

The strict-typing pass also fixed a latent bug: the prior code passed
``content=str`` to ``Message(...)`` but ``Message.content`` is
``list[ContentBlock]``. Messages are now built via a ``ContentBlock``
wrapper so the structured invariant holds.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from obscura.core.context import load_session_messages
from obscura.core.types import ContentBlock, Message, Role


def _write_events(db_path: Path, session_id: str, events: list[tuple[str, dict]]) -> None:
    """Seed an events.db with the given (kind, payload) tuples for *session_id*."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            kind TEXT,
            payload TEXT
        )
        """
    )
    for kind, payload in events:
        conn.execute(
            "INSERT INTO events (session_id, kind, payload) VALUES (?, ?, ?)",
            (session_id, kind, json.dumps(payload)),
        )
    conn.commit()
    conn.close()


def test_returns_empty_list_when_db_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no.db"
    assert load_session_messages("any", missing) == []


def test_returns_empty_list_when_session_has_no_events(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    _write_events(db, "other-session", [("user_message", {"content": "ignored"})])
    assert load_session_messages("missing", db) == []


def test_user_message_becomes_text_content_block(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    _write_events(db, "s", [("user_message", {"content": "hello"})])

    msgs = load_session_messages("s", db)

    assert len(msgs) == 1
    assert isinstance(msgs[0], Message)
    assert msgs[0].role == Role.USER
    # Critical invariant: content must be list[ContentBlock], not raw str
    assert isinstance(msgs[0].content, list)
    assert len(msgs[0].content) == 1
    block = msgs[0].content[0]
    assert isinstance(block, ContentBlock)
    assert block.kind == "text"
    assert block.text == "hello"


def test_text_deltas_concatenate_into_assistant_message_on_turn_complete(
    tmp_path: Path,
) -> None:
    db = tmp_path / "events.db"
    _write_events(
        db,
        "s",
        [
            ("user_message", {"content": "ask"}),
            ("text_delta", {"text": "He"}),
            ("text_delta", {"text": "llo"}),
            ("text_delta", {"text": " world"}),
            ("turn_complete", {}),
        ],
    )

    msgs = load_session_messages("s", db)

    assert [m.role for m in msgs] == [Role.USER, Role.ASSISTANT]
    assert msgs[1].content[0].text == "Hello world"


def test_pending_assistant_text_flushed_on_next_user_message(tmp_path: Path) -> None:
    """If turn_complete is missing but a new user_message arrives, flush deltas."""
    db = tmp_path / "events.db"
    _write_events(
        db,
        "s",
        [
            ("user_message", {"content": "first"}),
            ("text_delta", {"text": "partial"}),
            ("user_message", {"content": "second"}),
        ],
    )

    msgs = load_session_messages("s", db)
    roles = [m.role for m in msgs]

    assert roles == [Role.USER, Role.ASSISTANT, Role.USER]
    assert msgs[1].content[0].text == "partial"


def test_max_turns_keeps_only_recent_pairs(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    events = []
    for i in range(5):
        events.append(("user_message", {"content": f"u{i}"}))
        events.append(("text_delta", {"text": f"a{i}"}))
        events.append(("turn_complete", {}))
    _write_events(db, "s", events)

    msgs = load_session_messages("s", db, max_turns=2)

    # 2 turns = 4 messages (user+assistant pairs)
    assert len(msgs) == 4
    # Should be the LAST 2 turns
    assert msgs[0].content[0].text == "u3"
    assert msgs[-1].content[0].text == "a4"


def test_empty_user_message_payload_is_skipped(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    _write_events(
        db,
        "s",
        [
            ("user_message", {"content": ""}),
            ("user_message", {"content": "real"}),
        ],
    )

    msgs = load_session_messages("s", db)

    assert len(msgs) == 1
    assert msgs[0].content[0].text == "real"


def test_malformed_payload_is_silently_skipped(tmp_path: Path) -> None:
    """Per the implementation, JSON decode errors per-event don't fail the load."""
    db = tmp_path / "events.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, kind TEXT, payload TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO events (session_id, kind, payload) VALUES (?, ?, ?)",
        ("s", "user_message", "not valid json {"),
    )
    conn.execute(
        "INSERT INTO events (session_id, kind, payload) VALUES (?, ?, ?)",
        ("s", "user_message", json.dumps({"content": "ok"})),
    )
    conn.commit()
    conn.close()

    msgs = load_session_messages("s", db)
    assert len(msgs) == 1
    assert msgs[0].content[0].text == "ok"
