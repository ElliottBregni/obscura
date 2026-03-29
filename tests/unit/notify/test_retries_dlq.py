import pytest
import uuid
import sqlite3
from pathlib import Path
from obscura.notify.sqlite_impl import SQLiteStorage
from obscura.notify.storage import Message

PYTEST = pytest

@pytest.mark.asyncio
async def test_idempotency_and_dlq(tmp_path):
    db = tmp_path / "notify.db"
    url = f"sqlite:///{db}"
    s = SQLiteStorage(url)
    await s.setup()

    # create message with idempotency key
    mid1 = str(uuid.uuid4())
    key = "idem-123"
    msg1 = Message(id=mid1, user_id="u1", channel="slack", payload={"text":"hi"}, status="queued", idempotency_key=key)
    saved1 = await s.save_message(msg1)
    assert saved1 == mid1

    # attempt to save a different message with same idempotency key -> should return existing id
    mid2 = str(uuid.uuid4())
    msg2 = Message(id=mid2, user_id="u1", channel="slack", payload={"text":"hi again"}, status="queued", idempotency_key=key)
    saved2 = await s.save_message(msg2)
    assert saved2 == mid1

    # only one pending message should exist
    pend = await s.list_pending()
    assert sum(1 for m in pend if m.id == mid1) == 1

    # simulate retries and failures
    await s.update_status(mid1, "failed", attempts=1, last_error="err1")
    m1 = await s.get_message(mid1)
    assert m1 is not None and m1.attempts == 1

    await s.update_status(mid1, "failed", attempts=2, last_error="err2")
    m2 = await s.get_message(mid1)
    assert m2 is not None and m2.attempts == 2

    # exceed max attempts -> should move to dead_letters and be removed from messages
    await s.update_status(mid1, "failed", attempts=3, last_error="final")
    m3 = await s.get_message(mid1)
    assert m3 is None

    # verify dead_letters contains original_id
    con = sqlite3.connect(str(db))
    cur = con.execute("SELECT original_id, attempts, reason FROM dead_letters WHERE original_id=?", (mid1,))
    row = cur.fetchone()
    con.close()
    assert row is not None
    assert row[0] == mid1
    assert row[1] == 3
    assert "final" in (row[2] or "")

    await s.close()
