"""Tests for IMessageClient."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from obscura.integrations.imessage.client import (
    IMessage,
    IMessageClient,
    _apple_date_to_datetime,
)


class TestAppleDateConversion:
    def test_zero_returns_epoch(self) -> None:
        dt = _apple_date_to_datetime(0)
        assert dt == datetime.fromtimestamp(0, tz=timezone.utc)

    def test_none_returns_epoch(self) -> None:
        dt = _apple_date_to_datetime(None)
        assert dt == datetime.fromtimestamp(0, tz=timezone.utc)

    def test_nanosecond_format(self) -> None:
        # 2024-01-01 00:00:00 UTC in Apple nanoseconds
        # Unix: 1704067200, Apple seconds: 1704067200 - 978307200 = 725760000
        # Apple ns: 725760000 * 1_000_000_000
        apple_ns = 725760000 * 1_000_000_000
        dt = _apple_date_to_datetime(apple_ns)
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 1

    def test_second_format(self) -> None:
        # Small value treated as seconds since Apple epoch
        apple_secs = 725760000  # 2024-01-01
        dt = _apple_date_to_datetime(apple_secs)
        assert dt.year == 2024


class TestIMessageDataclass:
    def test_frozen(self) -> None:
        msg = IMessage(
            rowid=1,
            guid="abc",
            text="hello",
            sender="+1234567890",
            date=datetime.now(tz=timezone.utc),
            is_from_me=False,
        )
        with pytest.raises(AttributeError):
            msg.text = "changed"  # type: ignore[misc]


class TestIMessageClientCheckAccess:
    @pytest.mark.asyncio
    async def test_returns_false_for_nonexistent_db(self, tmp_path: Path) -> None:
        client = IMessageClient(["+1"], db_path=tmp_path / "nonexistent.db")
        result = await client.check_access()
        assert result is False
        assert client._use_sqlite is False

    @pytest.mark.asyncio
    async def test_returns_true_for_valid_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        con = sqlite3.connect(str(db_path))
        con.execute(
            "CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT)"
        )
        con.execute("INSERT INTO message (text) VALUES ('test')")
        con.commit()
        con.close()

        client = IMessageClient(["+1"], db_path=db_path)
        result = await client.check_access()
        assert result is True


class TestIMessageClientSend:
    @pytest.mark.asyncio
    async def test_send_calls_osascript(self) -> None:
        client = IMessageClient(["+1234567890"])
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            result = await client.send_message("+1234567890", "Hello")
            assert result is True
            mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_returns_false_on_failure(self) -> None:
        client = IMessageClient(["+1234567890"])
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"error")
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await client.send_message("+1234567890", "Hello")
            assert result is False

    @pytest.mark.asyncio
    async def test_send_escapes_quotes(self) -> None:
        client = IMessageClient(["+1"])
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await client.send_message("+1", 'He said "hello"')
            # Verify the script was passed to osascript
            call_args = mock_exec.call_args
            script = call_args[0][2]  # third positional arg is the script
            assert '\\"hello\\"' in script

    @pytest.mark.asyncio
    async def test_send_returns_false_on_timeout(self) -> None:
        client = IMessageClient(["+1234567890"])
        mock_proc = AsyncMock()

        async def _hang() -> tuple[bytes, bytes]:
            await asyncio.sleep(60)
            return (b"", b"")

        mock_proc.communicate.side_effect = _hang
        mock_proc.returncode = 0
        mock_proc.kill = Mock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await client.send_message("+1234567890", "Hello")
            assert result is False
            mock_proc.kill.assert_called_once()


class TestIMessageClientSQLiteRead:
    @pytest.mark.asyncio
    async def test_poll_unread_returns_messages(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        con = sqlite3.connect(str(db_path))
        con.execute("""
            CREATE TABLE handle (
                ROWID INTEGER PRIMARY KEY,
                id TEXT
            )
        """)
        con.execute("""
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                text TEXT,
                is_from_me INTEGER,
                date INTEGER,
                handle_id INTEGER
            )
        """)
        con.execute("INSERT INTO handle (ROWID, id) VALUES (1, '+1234567890')")
        # Apple date for 2024-01-01 in nanoseconds
        apple_ns = 725760000 * 1_000_000_000
        con.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id) "
            "VALUES (1, 'guid-1', 'Hello!', 0, ?, 1)",
            (apple_ns,),
        )
        con.commit()
        con.close()

        client = IMessageClient(["+1234567890"], db_path=db_path)
        messages = await client.poll_unread(0)
        assert len(messages) == 1
        assert messages[0].text == "Hello!"
        assert messages[0].sender == "+1234567890"
        assert messages[0].rowid == 1

    @pytest.mark.asyncio
    async def test_poll_filters_by_since_rowid(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
        con.execute("""
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT,
                is_from_me INTEGER, date INTEGER, handle_id INTEGER
            )
        """)
        con.execute("INSERT INTO handle (ROWID, id) VALUES (1, '+1')")
        con.execute(
            "INSERT INTO message VALUES (1, 'g1', 'old', 0, 0, 1)"
        )
        con.execute(
            "INSERT INTO message VALUES (2, 'g2', 'new', 0, 0, 1)"
        )
        con.commit()
        con.close()

        client = IMessageClient(["+1"], db_path=db_path)
        messages = await client.poll_unread(1)  # since_rowid=1 → only rowid>1
        assert len(messages) == 1
        assert messages[0].text == "new"

    @pytest.mark.asyncio
    async def test_poll_ignores_is_from_me(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
        con.execute("""
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT,
                is_from_me INTEGER, date INTEGER, handle_id INTEGER
            )
        """)
        con.execute("INSERT INTO handle (ROWID, id) VALUES (1, '+1')")
        con.execute("INSERT INTO message VALUES (1, 'g1', 'from me', 1, 0, 1)")
        con.execute("INSERT INTO message VALUES (2, 'g2', 'from them', 0, 0, 1)")
        con.commit()
        con.close()

        client = IMessageClient(["+1"], db_path=db_path)
        messages = await client.poll_unread(0)
        assert len(messages) == 1
        assert messages[0].text == "from them"

    @pytest.mark.asyncio
    async def test_poll_rechecks_access_and_recovers(self) -> None:
        client = IMessageClient(["+1"])
        client._use_sqlite = False
        client._next_access_recheck_at = 0.0
        client._next_warn_at = 9999.0
        expected = [
            IMessage(
                rowid=1,
                guid="g",
                text="hi",
                sender="+1",
                date=datetime.now(tz=timezone.utc),
                is_from_me=False,
            )
        ]
        with patch("time.monotonic", return_value=1.0):
            with patch.object(client, "check_access", AsyncMock(return_value=True)):
                with patch.object(client, "_poll_sqlite", AsyncMock(return_value=expected)):
                    out = await client.poll_unread(0)
        assert out == expected

    @pytest.mark.asyncio
    async def test_poll_rate_limits_disabled_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        client = IMessageClient(["+1"])
        client._use_sqlite = False
        client._next_access_recheck_at = 1000.0
        client._next_warn_at = 0.0
        with patch("time.monotonic", side_effect=[10.0, 10.5]):
            await client.poll_unread(0)
            await client.poll_unread(0)
        warnings = [
            r for r in caplog.records if "ingest disabled" in r.getMessage()
        ]
        assert len(warnings) == 1
