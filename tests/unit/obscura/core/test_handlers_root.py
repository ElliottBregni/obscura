"""Tests for sdk.handlers — RequestHandler protocol and SimpleHandler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.core.types import Backend, ContentBlock, Message, Role
from obscura.core.handlers import RequestHandler, SimpleHandler


# ---------------------------------------------------------------------------
# RequestHandler protocol
# ---------------------------------------------------------------------------


class TestRequestHandlerProtocol:
    def test_simple_handler_is_request_handler(self):
        client = MagicMock()
        handler = SimpleHandler(client)
        assert isinstance(handler, RequestHandler)

    def test_custom_handler_satisfies_protocol(self) -> None:
        class MyHandler:
            async def handle(self, request: object) -> str:
                return "ok"

        assert isinstance(MyHandler(), RequestHandler)

    def test_non_handler_fails_protocol(self):
        class NotAHandler:
            pass

        assert not isinstance(NotAHandler(), RequestHandler)


# ---------------------------------------------------------------------------
# SimpleHandler
# ---------------------------------------------------------------------------


class TestSimpleHandler:
    @pytest.mark.asyncio
    async def test_sends_prompt_to_client(self):
        mock_message = Message(
            role=Role.ASSISTANT,
            content=[ContentBlock(kind="text", text="Response here")],
            backend=Backend.COPILOT,
        )
        client = MagicMock()
        client.send = AsyncMock(return_value=mock_message)

        handler = SimpleHandler(client)
        result = await handler.handle("What is 2+2?")

        client.send.assert_called_once_with("What is 2+2?")
        assert result.text == "Response here"

    @pytest.mark.asyncio
    async def test_prepends_system_prompt(self):
        mock_message = Message(
            role=Role.ASSISTANT,
            content=[ContentBlock(kind="text", text="4")],
            backend=Backend.COPILOT,
        )
        client = MagicMock()
        client.send = AsyncMock(return_value=mock_message)

        handler = SimpleHandler(client, system_prompt="You are a math tutor.")
        await handler.handle("What is 2+2?")

        call_args = client.send.call_args[0][0]
        assert call_args.startswith("You are a math tutor.")
        assert "What is 2+2?" in call_args

    @pytest.mark.asyncio
    async def test_converts_input_to_string(self):
        mock_message = Message(
            role=Role.ASSISTANT,
            content=[ContentBlock(kind="text", text="ok")],
            backend=Backend.COPILOT,
        )
        client = MagicMock()
        client.send = AsyncMock(return_value=mock_message)

        handler = SimpleHandler(client)
        await handler.handle(42)

        client.send.assert_called_once_with("42")


