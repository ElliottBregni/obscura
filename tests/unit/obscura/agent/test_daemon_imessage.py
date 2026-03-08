"""Tests for IMessageTrigger and daemon iMessage handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from obscura.agent.daemon_agent import (
    DaemonAgent,
    IMessageTrigger,
    Trigger,
)
from obscura.agent.interaction import AttentionPriority


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
            "obscura.integrations.imessage.IMessageClient"
        ) as MockClient:
            mock_im_client = AsyncMock()
            mock_im_client.send_message.return_value = True
            MockClient.return_value = mock_im_client

            await daemon._handle_trigger(trigger)

            # Verify agent loop was called with prompt containing the message
            mock_client.run_loop_to_completion.assert_called_once()
            prompt = mock_client.run_loop_to_completion.call_args[0][0]
            assert "+1234567890" in prompt
            assert "hey there" in prompt

            # Verify reply was sent
            mock_im_client.send_message.assert_called_once_with(
                "+1234567890", "Sure!"
            )

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
