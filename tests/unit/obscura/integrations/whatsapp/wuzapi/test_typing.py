"""Tests for `_TypingTracker` — WhatsApp typing indicator with keepalive.

The tracker calls ``WuzapiClient.set_chat_presence`` with state=composing
on start (plus every refresh_interval_s) and state=paused on stop or
max-duration. All presence errors must be swallowed — the bubble is
best-effort and must never block a real reply.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.integrations.whatsapp.wuzapi.service import _TypingTracker


def _make_client() -> Any:
    """Mock WuzapiClient with an AsyncMock set_chat_presence."""
    client = MagicMock()
    client.set_chat_presence = AsyncMock(return_value=None)
    return client


def _states_sent(client: Any) -> list[str]:
    """Extract the ``state`` arg from every set_chat_presence call."""
    return [call.kwargs["state"] for call in client.set_chat_presence.call_args_list]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_sends_composing_immediately() -> None:
    """The first composing fires synchronously inside start() so the
    bubble appears as soon as the message hits the queue."""
    client = _make_client()
    tracker = _TypingTracker(client, refresh_interval_s=60.0)
    await tracker.start("alice@s.whatsapp.net")
    client.set_chat_presence.assert_awaited_once_with(
        "alice@s.whatsapp.net", state="composing",
    )
    tracker.cancel_all()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    """Calling start twice doesn't double-fire composing."""
    client = _make_client()
    tracker = _TypingTracker(client, refresh_interval_s=60.0)
    await tracker.start("alice@s.whatsapp.net")
    await tracker.start("alice@s.whatsapp.net")
    assert client.set_chat_presence.await_count == 1
    tracker.cancel_all()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_sends_paused_and_cancels_keepalive() -> None:
    """stop() ends the keepalive and clears the indicator."""
    client = _make_client()
    tracker = _TypingTracker(client, refresh_interval_s=60.0)
    await tracker.start("alice@s.whatsapp.net")
    await tracker.stop("alice@s.whatsapp.net")
    states = _states_sent(client)
    assert states == ["composing", "paused"]
    assert "alice@s.whatsapp.net" not in tracker._tasks


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_with_no_active_tracker_still_sends_paused() -> None:
    """stop() on a recipient that was never start()ed still sends
    paused — useful for clearing stale indicators from previous runs."""
    client = _make_client()
    tracker = _TypingTracker(client)
    await tracker.stop("alice@s.whatsapp.net")
    client.set_chat_presence.assert_awaited_once_with(
        "alice@s.whatsapp.net", state="paused",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_keepalive_refreshes_composing() -> None:
    """Keepalive re-sends composing every refresh_interval_s."""
    client = _make_client()
    tracker = _TypingTracker(
        client, refresh_interval_s=0.02, max_duration_s=10.0,
    )
    await tracker.start("alice@s.whatsapp.net")
    # Let several refreshes fire
    await asyncio.sleep(0.1)
    composing_count = sum(1 for s in _states_sent(client) if s == "composing")
    assert composing_count >= 3
    tracker.cancel_all()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_keepalive_auto_clears_on_max_duration() -> None:
    """When max_duration_s elapses, the keepalive sends paused and exits
    without anyone calling stop()."""
    client = _make_client()
    tracker = _TypingTracker(
        client, refresh_interval_s=0.01, max_duration_s=0.05,
    )
    await tracker.start("alice@s.whatsapp.net")
    # Wait past the max duration
    await asyncio.sleep(0.2)
    states = _states_sent(client)
    assert states[0] == "composing"
    assert "paused" in states
    assert "alice@s.whatsapp.net" not in tracker._tasks


@pytest.mark.unit
@pytest.mark.asyncio
async def test_presence_errors_are_swallowed() -> None:
    """A failing set_chat_presence (network blip, wuzapi down) does not
    raise from start/stop — typing is best-effort."""
    client = MagicMock()
    client.set_chat_presence = AsyncMock(
        side_effect=RuntimeError("wuzapi unreachable"),
    )
    tracker = _TypingTracker(client, refresh_interval_s=60.0)
    # Both of these would raise without exception suppression
    await tracker.start("alice@s.whatsapp.net")
    await tracker.stop("alice@s.whatsapp.net")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multiple_recipients_tracked_independently() -> None:
    """Each recipient has its own keepalive task; stopping one doesn't
    affect another."""
    client = _make_client()
    tracker = _TypingTracker(client, refresh_interval_s=60.0)
    await tracker.start("alice@s.whatsapp.net")
    await tracker.start("bob@s.whatsapp.net")
    assert len(tracker._tasks) == 2
    await tracker.stop("alice@s.whatsapp.net")
    assert "alice@s.whatsapp.net" not in tracker._tasks
    assert "bob@s.whatsapp.net" in tracker._tasks
    tracker.cancel_all()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_all_clears_all_tasks() -> None:
    """cancel_all() ends every keepalive — used at service shutdown."""
    client = _make_client()
    tracker = _TypingTracker(client, refresh_interval_s=60.0)
    await tracker.start("alice@s.whatsapp.net")
    await tracker.start("bob@s.whatsapp.net")
    tracker.cancel_all()
    assert len(tracker._tasks) == 0
