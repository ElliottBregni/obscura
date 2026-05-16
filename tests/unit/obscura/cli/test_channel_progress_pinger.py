"""Tests for `_channel_progress_pinger` — periodic 'still working' pings
sent back to the messaging channel during long-running agent turns.

Key invariants:
* Quiet for short turns: nothing fires before the initial delay elapses.
* On a long turn, pings cycle through the rotation roughly every interval.
* ``done_event.set()`` causes a clean exit even mid-loop.
* Exceptions from ``progress_fn`` (a flaky platform send) must NOT kill
  the pinger — it keeps trying.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from obscura.cli import _repl_loop


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pinger_quiet_when_done_before_initial_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the turn finishes inside the initial delay, no ping is sent."""
    monkeypatch.setattr(_repl_loop, "_CHANNEL_PROGRESS_INITIAL_DELAY_S", 0.5)
    monkeypatch.setattr(_repl_loop, "_CHANNEL_PROGRESS_INTERVAL_S", 0.5)

    progress: Any = AsyncMock(return_value=True)
    done = asyncio.Event()

    pinger = asyncio.create_task(
        _repl_loop._channel_progress_pinger(progress, done),
    )
    await asyncio.sleep(0.05)
    done.set()
    await pinger

    progress.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pinger_sends_pings_on_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the initial delay, pings fire approximately every interval."""
    monkeypatch.setattr(_repl_loop, "_CHANNEL_PROGRESS_INITIAL_DELAY_S", 0.05)
    monkeypatch.setattr(_repl_loop, "_CHANNEL_PROGRESS_INTERVAL_S", 0.05)

    progress: Any = AsyncMock(return_value=True)
    done = asyncio.Event()

    pinger = asyncio.create_task(
        _repl_loop._channel_progress_pinger(progress, done),
    )
    # Give it time for several intervals
    await asyncio.sleep(0.3)
    done.set()
    await pinger

    assert progress.await_count >= 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pinger_message_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pings cycle through the rotation rather than always the same string."""
    monkeypatch.setattr(_repl_loop, "_CHANNEL_PROGRESS_INITIAL_DELAY_S", 0.01)
    monkeypatch.setattr(_repl_loop, "_CHANNEL_PROGRESS_INTERVAL_S", 0.01)

    progress: Any = AsyncMock(return_value=True)
    done = asyncio.Event()

    pinger = asyncio.create_task(
        _repl_loop._channel_progress_pinger(progress, done),
    )
    await asyncio.sleep(0.2)
    done.set()
    await pinger

    # Extract the message strings sent
    sent_messages = [call.args[0] for call in progress.await_args_list]
    assert len(sent_messages) >= 2
    # At least two distinct messages should have been used (rotation)
    assert len(set(sent_messages)) >= 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pinger_swallows_progress_fn_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing progress_fn must NOT kill the pinger — keep trying."""
    monkeypatch.setattr(_repl_loop, "_CHANNEL_PROGRESS_INITIAL_DELAY_S", 0.01)
    monkeypatch.setattr(_repl_loop, "_CHANNEL_PROGRESS_INTERVAL_S", 0.01)

    progress: Any = AsyncMock(side_effect=RuntimeError("send failed"))
    done = asyncio.Event()

    pinger = asyncio.create_task(
        _repl_loop._channel_progress_pinger(progress, done),
    )
    await asyncio.sleep(0.1)
    done.set()
    await pinger

    # Pinger must have kept trying (multiple attempts) despite the
    # exception, not died on the first one.
    assert progress.await_count >= 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pinger_exits_cleanly_on_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """task.cancel() must exit the pinger without raising."""
    monkeypatch.setattr(_repl_loop, "_CHANNEL_PROGRESS_INITIAL_DELAY_S", 60.0)
    monkeypatch.setattr(_repl_loop, "_CHANNEL_PROGRESS_INTERVAL_S", 60.0)

    progress: Any = AsyncMock(return_value=True)
    done = asyncio.Event()

    pinger = asyncio.create_task(
        _repl_loop._channel_progress_pinger(progress, done),
    )
    await asyncio.sleep(0.05)
    pinger.cancel()
    # Should return cleanly, not re-raise CancelledError
    await pinger


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pinger_done_set_during_interval_exits_promptly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When done_event is set while the pinger is sleeping between pings,
    it should exit promptly (within the wait_for window) rather than
    burning a full interval."""
    monkeypatch.setattr(_repl_loop, "_CHANNEL_PROGRESS_INITIAL_DELAY_S", 0.01)
    monkeypatch.setattr(_repl_loop, "_CHANNEL_PROGRESS_INTERVAL_S", 5.0)

    progress: Any = AsyncMock(return_value=True)
    done = asyncio.Event()

    pinger = asyncio.create_task(
        _repl_loop._channel_progress_pinger(progress, done),
    )
    # Let the first ping fire
    await asyncio.sleep(0.05)
    # Now set done while pinger is sleeping the 5s interval
    done.set()
    # Should return almost immediately (well under the 5s interval)
    await asyncio.wait_for(pinger, timeout=1.0)
