import uuid

import pytest

from obscura.notify.sqlite_impl import SQLiteStorage
from obscura.notify.storage import Message


@pytest.mark.asyncio
async def test_sqlite_storage_basic(tmp_path) -> None:
    db = tmp_path / "notify.db"
    url = f"sqlite:///{db}"
    s = SQLiteStorage(url)
    await s.setup()
    mid = str(uuid.uuid4())
    msg = Message(
        id=mid,
        user_id="u1",
        channel="slack",
        payload={"text": "hi"},
        status="queued",
    )
    await s.save_message(msg)
    fetched = await s.get_message(mid)
    assert fetched is not None
    assert fetched.id == mid
    pending = await s.list_pending()
    assert any(m.id == mid for m in pending)
    await s.update_status(mid, "sent", attempts=1)
    f2 = await s.get_message(mid)
    assert f2.status == "sent"
    await s.close()
