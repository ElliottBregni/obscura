from __future__ import annotations

import time
from pathlib import Path

from obscura.integrations.messaging.identity import (
    build_conversation_key,
    normalize_identity,
)
from obscura.integrations.messaging.store import (
    ConversationStore,
    DaemonLockStore,
    MessageDedupeStore,
    MessageRuntimeEventStore,
    MessageSendEventStore,
)


def test_normalize_identity_phone_and_email() -> None:
    assert normalize_identity("tel:+1 (555) 123-4567") == "+15551234567"
    assert normalize_identity("+1-555-123-4567") == "+15551234567"
    assert normalize_identity("MAILTO:User@Example.com") == "user@example.com"


def test_conversation_key_stable_for_participant_order() -> None:
    k1 = build_conversation_key(
        platform="imessage",
        account_id="default",
        channel_id="dm:+15551234567",
        participants=["me", "+15551234567"],
    )
    k2 = build_conversation_key(
        platform="imessage",
        account_id="default",
        channel_id="dm:+15551234567",
        participants=["+15551234567", "me"],
    )
    assert k1 == k2


def test_conversation_store_and_dedupe_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "messaging_state.db"

    conv = ConversationStore(db_path=db_path)
    state = conv.ensure(
        conversation_key="abc",
        platform="imessage",
        account_id="default",
        channel_id="dm:+1555",
        participants=["me", "+1555"],
    )
    assert state.conversation_key == "abc"

    state = conv.append_user_message("abc", "hello")
    state = conv.append_assistant_message("abc", "hey")
    assert conv.user_turn_count(state) == 1
    assert len(state.history) == 2

    # Force staleness and ensure reset clears history.
    conv.set_last_activity("abc", time.time() - 7200)
    assert conv.reset_if_stale("abc", timeout_seconds=3600) is True
    assert conv.get("abc") is not None
    assert conv.get("abc").history == []  # type: ignore[union-attr]

    dedupe = MessageDedupeStore(db_path=db_path)
    assert dedupe.contains("imessage:1") is False
    dedupe.add("imessage:1")
    assert dedupe.contains("imessage:1") is True
    assert dedupe.add_if_absent("imessage:2") is True
    assert dedupe.add_if_absent("imessage:2") is False

    send_events = MessageSendEventStore(db_path=db_path)
    send_events.add(
        platform="imessage",
        conversation_key="abc",
        recipient="+1555",
        success=True,
        reply_text="ok",
    )
    runtime_events = MessageRuntimeEventStore(db_path=db_path)
    runtime_events.add(
        component="imessage-assistant",
        event_type="send_ok",
        platform="imessage",
        conversation_key="abc",
        message_id="m1",
        details={"recipient": "+1555"},
    )

    import sqlite3

    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute(
            "SELECT recipient, success, reply_preview FROM messaging_send_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row == ("+1555", 1, "ok")
        row2 = con.execute(
            "SELECT component, event_type, platform, conversation_key, message_id FROM messaging_runtime_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row2 == ("imessage-assistant", "send_ok", "imessage", "abc", "m1")
    finally:
        con.close()


def test_daemon_lock_store_takeover_and_release(tmp_path: Path) -> None:
    db_path = tmp_path / "messaging_state.db"
    locks = DaemonLockStore(db_path=db_path)

    assert locks.try_acquire(lock_name="daemon:imessage", owner_id="a", stale_after_s=300.0) is True
    assert locks.try_acquire(lock_name="daemon:imessage", owner_id="b", stale_after_s=300.0) is False
    assert locks.heartbeat(lock_name="daemon:imessage", owner_id="a") is True
    assert locks.heartbeat(lock_name="daemon:imessage", owner_id="b") is False

    # Force stale lock then ensure takeover.
    import sqlite3

    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "UPDATE messaging_daemon_locks SET heartbeat_at_epoch_s = ? WHERE lock_name = ?",
            (time.time() - 1000.0, "daemon:imessage"),
        )
        con.commit()
    finally:
        con.close()

    assert locks.try_acquire(lock_name="daemon:imessage", owner_id="b", stale_after_s=30.0) is True
    locks.release(lock_name="daemon:imessage", owner_id="b")
    assert locks.try_acquire(lock_name="daemon:imessage", owner_id="c", stale_after_s=30.0) is True
