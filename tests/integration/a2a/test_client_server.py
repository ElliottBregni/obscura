"""Integration tests: A2AClient ↔ A2A server roundtrip.

Exercises the high-level ``A2AClient`` API against a real FastAPI server
(via httpx ASGI transport).  Agent execution is patched out so every
task completes instantly without calling any LLM.

Scenarios:
- discover() → fetches a valid AgentCard
- send_message() → returns a COMPLETED task with text artifact
- get_task() → retrieves by ID
- list_tasks() → paginates by contextId
- cancel_task() → transitions a PENDING task to CANCELED
- multi-turn context → two messages under the same contextId
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from obscura.core.enums.protocol import A2ARole, A2ATaskState
from obscura.integrations.a2a.client import A2AClient, A2ASessionManager
from obscura.integrations.a2a.store import InMemoryTaskStore
from obscura.integrations.a2a.types import A2AMessage, TextPart

FAKE_RESPONSE = "The test agent says hello."
TEST_AGENT_NAME = "integration-test-agent"
TEST_AGENT_URL = "http://testserver"

pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Fixture: A2AClient wired to the in-process test app
# ---------------------------------------------------------------------------


@pytest.fixture()
async def a2a_client(app, patch_session) -> AsyncIterator[A2AClient]:
    """A2AClient using ASGI transport — no real network, no LLM calls."""
    transport = httpx.ASGITransport(app=app)
    client = A2AClient(TEST_AGENT_URL, transport=transport)
    await client.connect()
    yield client
    await client.disconnect()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_discover_returns_agent_card(a2a_client: A2AClient) -> None:
    """discover() must fetch and return a valid AgentCard."""
    card = await a2a_client.discover()
    assert card.name == TEST_AGENT_NAME
    assert card.protocolVersion == "0.3"
    assert card.capabilities.streaming is True


@pytest.mark.asyncio
async def test_client_discover_caches_card(a2a_client: A2AClient) -> None:
    """Calling discover() twice returns the same card object via cache."""
    card1 = await a2a_client.discover()
    card2 = await a2a_client.discover()
    assert card1 == card2
    # The cached value should also be on the client property
    assert a2a_client.agent_card is not None
    assert a2a_client.agent_card.name == TEST_AGENT_NAME


# ---------------------------------------------------------------------------
# Message send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_send_message_blocking(a2a_client: A2AClient) -> None:
    """send_message(blocking=True) must return a COMPLETED task."""
    task = await a2a_client.send_message("hello from client", blocking=True)
    assert task.status.state == A2ATaskState.COMPLETED


@pytest.mark.asyncio
async def test_client_send_message_has_artifact(a2a_client: A2AClient) -> None:
    """Completed task must carry the fake agent response as an artifact."""
    task = await a2a_client.send_message("give me a response", blocking=True)
    assert len(task.artifacts) > 0

    text_values = [
        p.text for art in task.artifacts for p in art.parts if hasattr(p, "text")
    ]
    assert FAKE_RESPONSE in text_values


@pytest.mark.asyncio
async def test_client_send_message_non_blocking(a2a_client: A2AClient) -> None:
    """send_message(blocking=False) returns immediately — task may be PENDING."""
    task = await a2a_client.send_message("quick shot", blocking=False)
    # Non-blocking: task is created but may not have finished yet
    assert task.id.startswith("task-")
    assert task.status.state in (
        A2ATaskState.PENDING,
        A2ATaskState.WORKING,
        A2ATaskState.COMPLETED,
    )


# ---------------------------------------------------------------------------
# Task retrieval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_get_task(a2a_client: A2AClient) -> None:
    """get_task() retrieves a task by ID from the server."""
    created = await a2a_client.send_message("create then get", blocking=True)
    fetched = await a2a_client.get_task(created.id)
    assert fetched.id == created.id
    assert fetched.status.state == A2ATaskState.COMPLETED


# ---------------------------------------------------------------------------
# Task listing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_list_tasks_by_context(a2a_client: A2AClient) -> None:
    """list_tasks(context_id=…) returns all tasks under that context."""
    ctx = f"ctx-list-{uuid.uuid4().hex[:8]}"
    await a2a_client.send_message("msg1", context_id=ctx, blocking=True)
    await a2a_client.send_message("msg2", context_id=ctx, blocking=True)

    tasks, next_cursor = await a2a_client.list_tasks(context_id=ctx)
    assert len(tasks) == 2
    assert all(t.contextId == ctx for t in tasks)


@pytest.mark.asyncio
async def test_client_list_tasks_empty_context(a2a_client: A2AClient) -> None:
    """list_tasks for an unused context returns an empty list."""
    tasks, _ = await a2a_client.list_tasks(context_id="ctx-nonexistent-xyz")
    assert tasks == []


# ---------------------------------------------------------------------------
# Task cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_cancel_pending_task(
    a2a_client: A2AClient, store: InMemoryTaskStore
) -> None:
    """cancel_task() transitions a PENDING task to CANCELED."""
    # Create in store directly to keep it PENDING (no agent run)
    pending = await store.create_task(
        context_id="ctx-client-cancel",
        initial_message=A2AMessage(
            role=A2ARole.USER,
            messageId="m-client-cancel",
            parts=[TextPart(text="cancel me via client")],
        ),
    )

    canceled = await a2a_client.cancel_task(pending.id)
    assert canceled.status.state == A2ATaskState.CANCELED


# ---------------------------------------------------------------------------
# Multi-turn context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_multi_turn_same_context(a2a_client: A2AClient) -> None:
    """Two messages in the same context_id produce two tasks under that context."""
    ctx = f"ctx-mt-{uuid.uuid4().hex[:8]}"
    task1 = await a2a_client.send_message("turn 1", context_id=ctx, blocking=True)
    task2 = await a2a_client.send_message("turn 2", context_id=ctx, blocking=True)

    assert task1.contextId == ctx
    assert task2.contextId == ctx
    assert task1.id != task2.id

    all_tasks, _ = await a2a_client.list_tasks(context_id=ctx)
    assert len(all_tasks) == 2


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_manager_add_and_get(app) -> None:
    """A2ASessionManager tracks named client sessions."""
    transport = httpx.ASGITransport(app=app)
    mgr = A2ASessionManager()

    # We can't pass transport via A2ASessionManager.add, so create manually
    client = A2AClient(TEST_AGENT_URL, transport=transport)
    await client.connect()
    mgr._clients["my-agent"] = client  # internal wiring for test

    assert mgr.get("my-agent") is client
    assert "my-agent" in mgr.list_sessions()

    await mgr.close_all()
    assert mgr.list_sessions() == []
