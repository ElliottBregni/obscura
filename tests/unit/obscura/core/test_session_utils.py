"""Tests for session_utils — title generation and isolation guarantees.

The critical invariant: ``generate_session_title`` must NOT pollute the
live conversation history. For Copilot in particular, every
``backend.send`` persists into the session's server-side state, so
title-gen has to go through ``send_isolated`` (a temp session) when
available. Other backends are stateless or HTTP-per-call and need no
isolation.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.core.session_utils import generate_session_title


def _make_response(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


class TestGenerateSessionTitle:
    async def test_uses_send_isolated_when_available(self) -> None:
        backend = MagicMock()
        backend.send_isolated = AsyncMock(return_value=_make_response("My Cool Title"))
        backend.send = AsyncMock(return_value=_make_response("WRONG path"))

        title = await generate_session_title("refactor saved-search batch list", backend)

        assert title == "My Cool Title"
        backend.send_isolated.assert_awaited_once()
        # Critical: the live ``send`` path must NOT be hit. Calling it
        # would persist the title-gen prompt into the active Copilot
        # session and contaminate the next user turn.
        backend.send.assert_not_called()

    async def test_falls_back_to_send_when_no_isolated(self) -> None:
        backend = MagicMock(spec=["send"])  # no send_isolated attribute
        backend.send = AsyncMock(return_value=_make_response("Title From Send"))

        title = await generate_session_title("some message about work", backend)

        assert title == "Title From Send"
        backend.send.assert_awaited_once()

    async def test_short_message_returns_empty_without_call(self) -> None:
        backend = MagicMock()
        backend.send_isolated = AsyncMock()
        backend.send = AsyncMock()

        title = await generate_session_title("hi", backend)

        assert title == ""
        backend.send_isolated.assert_not_called()
        backend.send.assert_not_called()

    async def test_strips_quotes_and_caps_length(self) -> None:
        long_title = "x" * 100
        backend = MagicMock()
        backend.send_isolated = AsyncMock(
            return_value=_make_response(f'"{long_title}"')
        )

        title = await generate_session_title("a real first message", backend)

        # Quotes stripped, length capped at 60 with ellipsis.
        assert not title.startswith('"')
        assert len(title) <= 60
        assert title.endswith("...")

    async def test_timeout_returns_empty(self) -> None:
        async def _slow(*_: Any, **__: Any) -> Any:
            import asyncio

            await asyncio.sleep(10)
            return _make_response("never")

        backend = MagicMock()
        backend.send_isolated = AsyncMock(side_effect=_slow)

        title = await generate_session_title(
            "a real first message",
            backend,
            timeout=0.01,
        )

        # Timeout caught silently — generator returns "".
        assert title == ""

    async def test_send_isolated_failure_falls_through_to_empty(self) -> None:
        """If the isolated send raises, we don't try ``send`` as a fallback —
        a successful title isn't worth polluting the conversation. Caller
        gets an empty string."""
        backend = MagicMock()
        backend.send_isolated = AsyncMock(side_effect=RuntimeError("boom"))
        backend.send = AsyncMock(return_value=_make_response("polluting fallback"))

        title = await generate_session_title("a real first message", backend)

        assert title == ""
        # We must NOT have leaked into the live conversation as a fallback.
        backend.send.assert_not_called()


@pytest.mark.parametrize(
    ("first", "expected"),
    [
        ("", True),
        ("    ", True),
        ("hi", True),  # under 5 chars
        ("hello", False),
    ],
)
async def test_short_or_empty_first_message_is_a_noop(
    first: str, expected: bool
) -> None:
    backend = MagicMock()
    backend.send_isolated = AsyncMock(return_value=_make_response("x"))
    title = await generate_session_title(first, backend)
    if expected:
        assert title == ""
        backend.send_isolated.assert_not_called()
    else:
        backend.send_isolated.assert_awaited_once()
