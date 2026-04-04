import os
import uuid

import pytest

pytestmark = pytest.mark.asyncio

NOTIFY_DB = os.environ.get("NOTIFY_DATABASE_URL", "")

skip_reason = "NOTIFY_DATABASE_URL not set to a postgres DSN or asyncpg not installed"

try:
    import asyncpg  # type: ignore
except Exception:
    asyncpg = None

should_run = asyncpg is not None and NOTIFY_DB.startswith("postgres")


@pytest.mark.skipif(not should_run, reason=skip_reason)
async def test_postgres_storage_basic(tmp_path) -> None:
    # runs only when NOTIFY_DATABASE_URL points to a postgres:// DSN and asyncpg is available
    from obscura.notify.postgres_impl import PostgresStorage
    from obscura.notify.storage import Message

    s = PostgresStorage(NOTIFY_DB)
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
