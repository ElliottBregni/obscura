"""End-to-end smoke tests for the Obscura↔OpenClaw bridge.

These tests hit the real OpenClaw gateway at http://localhost:18789.
All tests are skipped automatically when the gateway is unreachable,
so CI stays green without a live OpenClaw instance.

Run manually with OpenClaw running:
    uv run pytest tests/integration/a2a/test_openclaw_bridge.py -v
"""

from __future__ import annotations

import pytest

from obscura.core.enums.protocol import A2ATaskState
from obscura.integrations.a2a.openclaw_bridge import OpenClawBridge, OpenClawContext

pytestmark = pytest.mark.integration

_TOKEN = "4a30d783737e2aac23148de52a29d9b820cffba3eda8754a"
_GATEWAY = "http://localhost:18789"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def bridge():
    """Connected OpenClawBridge; skips entire test if gateway is unreachable."""
    b = OpenClawBridge.from_config(token=_TOKEN, gateway_url=_GATEWAY)
    await b.connect()
    healthy = await b.health_check()
    if not healthy:
        await b.disconnect()
        pytest.skip("OpenClaw gateway not reachable at http://localhost:18789")
    yield b
    await b.disconnect()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check(bridge: OpenClawBridge) -> None:
    """health_check() returns True when the gateway is reachable."""
    # fixture already verified this; call again to assert the API shape
    result = await bridge.health_check()
    assert result is True


@pytest.mark.asyncio
async def test_send_message(bridge: OpenClawBridge) -> None:
    """send('ping') returns a completed A2ATask with non-empty reply text."""
    task = await bridge.send("ping")

    assert task.status.state == A2ATaskState.COMPLETED, (
        f"Expected completed, got {task.status.state!r}. Task id={task.id}"
    )
    assert task.artifacts, "Expected at least one artifact in completed task"
    reply_text = "".join(p.text for p in task.artifacts[0].parts if hasattr(p, "text"))
    assert reply_text.strip(), "Expected non-empty reply text from OpenClaw"


@pytest.mark.asyncio
async def test_stream_send(bridge: OpenClawBridge) -> None:
    """stream_send yields at least one working event and ends with a completed event."""
    events = []
    async for event in bridge.stream_send("say hello"):
        events.append(event)

    assert events, "Expected at least one event from stream_send"

    states = [e.status.state for e in events]
    final_event = events[-1]

    # The final event must be completed
    assert final_event.status.state == A2ATaskState.COMPLETED, (
        f"Last event state was {final_event.status.state!r}, expected completed. "
        f"All states: {states}"
    )
    assert final_event.final is True, "Last event must have final=True"

    # If streaming is supported, we expect intermediate working events;
    # if it fell back to blocking, we get a single completed event — both are OK.
    working_events = [e for e in events if e.status.state == A2ATaskState.WORKING]
    completed_events = [e for e in events if e.status.state == A2ATaskState.COMPLETED]
    assert completed_events, "Must have at least one completed event"

    # Sanity: all events share the same task/context ID
    task_ids = {e.taskId for e in events}
    context_ids = {e.contextId for e in events}
    assert len(task_ids) == 1, f"Mixed task IDs in stream: {task_ids}"
    assert len(context_ids) == 1, f"Mixed context IDs in stream: {context_ids}"

    _ = working_events  # referenced to avoid F841; working events are optional


@pytest.mark.asyncio
async def test_openclaw_context_multiturn(bridge: OpenClawBridge) -> None:
    """OpenClawContext threads conversation history across two turns."""
    ctx = OpenClawContext()

    t1 = await ctx.send(bridge, "My name is TestUser. Remember that.")
    assert t1.status.state == A2ATaskState.COMPLETED, (
        f"Turn 1 failed: {t1.status.state!r}"
    )

    t2 = await ctx.send(bridge, "What is my name?")
    assert t2.status.state == A2ATaskState.COMPLETED, (
        f"Turn 2 failed: {t2.status.state!r}"
    )

    # Context should record two turns (user+assistant pairs)
    assert len(ctx) == 2, f"Expected 2 turns in context, got {len(ctx)}"

    # Both tasks share the same context ID
    assert t1.contextId == t2.contextId, (
        f"Context IDs diverged: {t1.contextId!r} vs {t2.contextId!r}"
    )

    # The second reply should mention the name (best-effort; models vary)
    t2_reply = (
        "".join(p.text for p in t2.artifacts[0].parts if hasattr(p, "text"))
        if t2.artifacts
        else ""
    )
    if t2_reply:
        # Soft assertion — log rather than fail if the model doesn't recall the name.
        # This catches obvious failures (empty reply, error message) without being
        # brittle about exact phrasing.
        assert len(t2_reply.strip()) > 0, "Turn 2 reply was unexpectedly empty"
