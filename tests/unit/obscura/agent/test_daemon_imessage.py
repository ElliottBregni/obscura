"""Tests for IMessageTrigger and daemon iMessage handling."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from obscura.agent.daemon_agent import (
    DaemonAgent,
    IMessageTrigger,
    MessageTrigger,
    Trigger,
)
from obscura.agent.interaction import AttentionPriority
from obscura.integrations.messaging.models import PlatformMessage


class TestIMessageTrigger:
    def test_defaults(self) -> None:
        t = IMessageTrigger(contacts=("+1234567890",))
        assert t.kind == "imessage"
        assert t.contacts == ("+1234567890",)
        assert t.poll_interval == 30

    def test_custom_interval(self) -> None:
        t = IMessageTrigger(
            contacts=("+1", "+2"),
            poll_interval=60,
            notify_user=True,
            priority=AttentionPriority.HIGH,
        )
        assert t.poll_interval == 60
        assert t.notify_user is True
        assert len(t.contacts) == 2

    def test_frozen(self) -> None:
        t = IMessageTrigger(contacts=("+1",))
        with pytest.raises(AttributeError):
            t.poll_interval = 99  # type: ignore[misc]


class TestDaemonIMessageHandling:
    @pytest.mark.asyncio
    async def test_loop_forever_alias_calls_run_forever(self) -> None:
        mock_client = AsyncMock()
        daemon = DaemonAgent(mock_client, name="test", triggers=[])
        daemon.run_forever = AsyncMock()  # type: ignore[method-assign]

        await daemon.loop_forever()

        daemon.run_forever.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_trigger_dispatches_imessage(self) -> None:
        mock_client = AsyncMock()
        mock_client.run_loop_to_completion.return_value = "Sure!"

        daemon = DaemonAgent(mock_client, name="test", triggers=[])

        trigger = Trigger(
            kind="imessage",
            data={
                "sender": "+1234567890",
                "text": "hey there",
                "guid": "abc",
                "date": "2024-01-01T00:00:00+00:00",
            },
            notify_user=False,
        )

        with patch(
            "obscura.integrations.imessage.IMessageAdapter"
        ) as MockAdapter:
            mock_adapter = AsyncMock()
            mock_adapter.send.return_value = True
            MockAdapter.return_value = mock_adapter

            await daemon._handle_trigger(trigger)

            # Verify agent loop was called with prompt containing the message
            mock_client.run_loop_to_completion.assert_called_once()
            prompt = mock_client.run_loop_to_completion.call_args[0][0]
            assert "+1234567890" in prompt
            assert "hey there" in prompt

            # Verify reply was sent
            mock_adapter.send.assert_called_once_with("+1234567890", "Sure!")

    @pytest.mark.asyncio
    async def test_handle_trigger_non_imessage_unchanged(self) -> None:
        mock_client = AsyncMock()
        mock_client.run_loop_to_completion.return_value = "done"

        daemon = DaemonAgent(mock_client, name="test", triggers=[])

        trigger = Trigger(
            kind="schedule",
            prompt="do something",
            notify_user=False,
        )

        await daemon._handle_trigger(trigger)
        mock_client.run_loop_to_completion.assert_called_once_with(
            "do something", max_turns=15,
        )

    @pytest.mark.asyncio
    async def test_multi_turn_persists_for_same_conversation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        mock_client = AsyncMock()
        mock_client.run_loop_to_completion.side_effect = ["first", "second"]

        daemon = DaemonAgent(mock_client, name="test", triggers=[])

        trig1 = Trigger(
            kind="imessage",
            data={
                "platform": "imessage",
                "account_id": "default",
                "channel_id": "dm:+15551234567",
                "conversation_key": "conv-1",
                "sender": "+15551234567",
                "sender_id": "+15551234567",
                "sender_target": "+15551234567",
                "text": "hello",
                "message_id": "m1",
            },
            notify_user=False,
        )
        trig2 = Trigger(
            kind="imessage",
            data={
                "platform": "imessage",
                "account_id": "default",
                "channel_id": "dm:+15551234567",
                "conversation_key": "conv-1",
                "sender": "tel:+1 (555) 123-4567",
                "sender_id": "+15551234567",
                "sender_target": "+15551234567",
                "text": "again",
                "message_id": "m2",
            },
            notify_user=False,
        )

        with patch("obscura.integrations.messaging.factory.get_adapter") as get_adapter:
            adapter = AsyncMock()
            adapter.send.return_value = True
            get_adapter.return_value = adapter

            await daemon._handle_trigger(trig1)
            await daemon._handle_trigger(trig2)

            state = daemon._conversation_store.get("conv-1")
            assert state is not None
            assert daemon._conversation_store.user_turn_count(state) == 2
            assert len(state.history) == 4

    @pytest.mark.asyncio
    async def test_forced_recipient_overrides_sender_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        mock_client = AsyncMock()
        mock_client.run_loop_to_completion.return_value = "ok"

        daemon = DaemonAgent(mock_client, name="test", triggers=[])
        trig = Trigger(
            kind="imessage",
            data={
                "platform": "imessage",
                "account_id": "default",
                "channel_id": "dm:+15551234567",
                "conversation_key": "conv-2",
                "sender": "+15551234567",
                "sender_id": "+15551234567",
                "sender_target": "+15551234567",
                "forced_recipient": "+12316333624",
                "text": "hello",
                "message_id": "m-force",
            },
            notify_user=False,
        )

        with patch("obscura.integrations.messaging.factory.get_adapter") as get_adapter:
            adapter = AsyncMock()
            adapter.send.return_value = True
            get_adapter.return_value = adapter
            await daemon._handle_trigger(trig)
            adapter.send.assert_called_once_with("+12316333624", "ok")

    @pytest.mark.asyncio
    async def test_send_timeout_records_failure_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        mock_client = AsyncMock()
        mock_client.run_loop_to_completion.return_value = "ok"
        daemon = DaemonAgent(mock_client, name="test", triggers=[])

        trig = Trigger(
            kind="imessage",
            data={
                "platform": "imessage",
                "account_id": "default",
                "channel_id": "dm:+15551234567",
                "conversation_key": "conv-3",
                "sender": "+15551234567",
                "sender_id": "+15551234567",
                "sender_target": "+15551234567",
                "text": "hello",
                "message_id": "m-timeout",
            },
            notify_user=False,
        )

        with patch("obscura.integrations.messaging.factory.get_adapter") as get_adapter:
            adapter = AsyncMock()
            adapter.send.side_effect = asyncio.TimeoutError
            get_adapter.return_value = adapter
            await daemon._handle_trigger(trig)

        import sqlite3

        con = sqlite3.connect(str(tmp_path / "messaging_state.db"))
        try:
            row = con.execute(
                "SELECT success, error_text FROM messaging_send_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert row == (0, "send_timeout")
            r2 = con.execute(
                "SELECT event_type FROM messaging_runtime_events ORDER BY id DESC LIMIT 3"
            ).fetchall()
            assert any(x[0] == "send_failed" for x in r2)
        finally:
            con.close()

    @pytest.mark.asyncio
    async def test_scheduler_crash_restarts_and_records_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        mock_client = AsyncMock()
        daemon = DaemonAgent(mock_client, name="test", triggers=[])

        async def _boom() -> None:
            raise RuntimeError("scheduler boom")

        calls = {"n": 0}

        async def _fake_next() -> Trigger | None:
            calls["n"] += 1
            if calls["n"] >= 2:
                daemon._stopped = True
            await asyncio.sleep(0)
            return None

        daemon._run_schedulers = _boom  # type: ignore[method-assign]
        daemon._get_next_trigger = _fake_next  # type: ignore[method-assign]

        await daemon.run_forever()

        import sqlite3

        con = sqlite3.connect(str(tmp_path / "messaging_state.db"))
        try:
            rows = con.execute(
                "SELECT event_type, details_json FROM messaging_runtime_events ORDER BY id DESC LIMIT 5"
            ).fetchall()
            assert any(r[0] == "scheduler_restarted" for r in rows)
        finally:
            con.close()

    @pytest.mark.asyncio
    async def test_trigger_timeout_is_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        mock_client = AsyncMock()
        daemon = DaemonAgent(mock_client, name="test", triggers=[])
        daemon._trigger_timeout_s = 0.01

        async def _slow_handle(_trigger: Trigger) -> None:
            await asyncio.sleep(0.2)

        daemon._handle_trigger = _slow_handle  # type: ignore[method-assign]
        await daemon.fire(Trigger(kind="manual", prompt="x"))

        async def _next_once() -> Trigger | None:
            if daemon._trigger_count > 0:
                daemon._stopped = True
                return None
            return await asyncio.wait_for(daemon._trigger_queue.get(), timeout=0.1)

        daemon._get_next_trigger = _next_once  # type: ignore[method-assign]
        daemon._run_schedulers = AsyncMock()  # type: ignore[method-assign]
        await daemon.run_forever()

        import sqlite3

        con = sqlite3.connect(str(tmp_path / "messaging_state.db"))
        try:
            rows = con.execute(
                "SELECT event_type FROM messaging_runtime_events ORDER BY id DESC LIMIT 20"
            ).fetchall()
            evs = {r[0] for r in rows}
            assert "trigger_timeout" in evs
            assert "trigger_enqueued" in evs
            assert "trigger_dequeued" in evs
        finally:
            con.close()

    @pytest.mark.asyncio
    async def test_message_trigger_uses_configured_max_turns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        mock_client = AsyncMock()
        mock_client.run_loop_to_completion.return_value = "ok"
        daemon = DaemonAgent(mock_client, name="test", triggers=[], max_turns_per_trigger=7)
        trig = Trigger(
            kind="imessage",
            data={
                "platform": "imessage",
                "account_id": "default",
                "channel_id": "dm:+15551234567",
                "conversation_key": "conv-max-turns",
                "sender": "+15551234567",
                "sender_id": "+15551234567",
                "sender_target": "+15551234567",
                "text": "hello",
                "message_id": "m-max-turns",
            },
            notify_user=False,
        )
        with patch("obscura.integrations.messaging.factory.get_adapter") as get_adapter:
            adapter = AsyncMock()
            adapter.send.return_value = True
            get_adapter.return_value = adapter
            await daemon._handle_trigger(trig)
        kwargs = mock_client.run_loop_to_completion.call_args.kwargs
        assert kwargs["max_turns"] == 7

    @pytest.mark.asyncio
    async def test_poll_messages_dedupes_duplicate_message_ids_atomically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        mock_client = AsyncMock()
        daemon = DaemonAgent(mock_client, name="test", triggers=[])

        m1 = PlatformMessage(
            platform="imessage",
            account_id="default",
            channel_id="dm:+15551234567",
            sender_id="+15551234567",
            recipient_id="me",
            message_id="dup-1",
            text="hello",
            timestamp=datetime.now(tz=timezone.utc),
            metadata={"sender_raw": "+15551234567", "sender_target": "+15551234567"},
        )
        m2 = PlatformMessage(
            platform="imessage",
            account_id="default",
            channel_id="dm:+15551234567",
            sender_id="+15551234567",
            recipient_id="me",
            message_id="dup-1",
            text="hello again",
            timestamp=datetime.now(tz=timezone.utc),
            metadata={"sender_raw": "+15551234567", "sender_target": "+15551234567"},
        )
        trigger = MessageTrigger(
            platform="imessage",
            contacts=("+15551234567",),
            poll_interval=1,
            account_id="default",
        )

        with patch("obscura.integrations.messaging.factory.get_adapter") as get_adapter:
            adapter = AsyncMock()
            adapter.start.return_value = None
            adapter.poll = AsyncMock(side_effect=[[m1, m2], []])
            get_adapter.return_value = adapter

            task = asyncio.create_task(daemon._poll_messages([trigger]))
            await asyncio.sleep(0.15)
            daemon._stopped = True
            await asyncio.wait_for(task, timeout=1.0)

        queued: list[Trigger] = []
        while not daemon._trigger_queue.empty():
            queued.append(daemon._trigger_queue.get_nowait())
        assert len(queued) == 1
        assert str(queued[0].data.get("message_id")) == "dup-1"
