"""Integration tests: A2A task creation, execution, retrieval, and cancellation.

Covers both the REST transport (/a2a/v1/tasks) and the JSON-RPC 2.0
transport (/a2a/rpc) against a fully assembled FastAPI app.

Agent execution is short-circuited by the ``patch_session`` fixture so
tasks complete instantly without touching any LLM backend.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from obscura.core.enums.protocol import A2ARole, A2ATaskState
from obscura.integrations.a2a.store import InMemoryTaskStore
from obscura.integrations.a2a.types import A2AMessage, TextPart

FAKE_RESPONSE = "The test agent says hello."


def user_message_dict(text: str = "ping") -> dict[str, Any]:
    return {
        "role": "user",
        "messageId": f"msg-{uuid.uuid4().hex[:8]}",
        "parts": [{"kind": "text", "text": text}],
    }

pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# REST transport — /a2a/v1/tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_create_task_returns_200(a2a_http, patch_session) -> None:
    """POST /a2a/v1/tasks creates a task and returns it with HTTP 200."""
    resp = await a2a_http.post(
        "/a2a/v1/tasks",
        json={"message": user_message_dict("What is 2+2?"), "blocking": True},
    )
    assert resp.status_code == 200
    task = resp.json()
    assert "id" in task
    assert task["id"].startswith("task-")


@pytest.mark.asyncio
async def test_rest_task_completes(a2a_http, patch_session) -> None:
    """A blocking task must reach COMPLETED state."""
    resp = await a2a_http.post(
        "/a2a/v1/tasks",
        json={"message": user_message_dict("hello"), "blocking": True},
    )
    assert resp.json()["status"]["state"] == A2ATaskState.COMPLETED.value


@pytest.mark.asyncio
async def test_rest_task_has_artifact(a2a_http, patch_session) -> None:
    """Completed task must carry at least one text artifact with the response."""
    resp = await a2a_http.post(
        "/a2a/v1/tasks",
        json={"message": user_message_dict("respond"), "blocking": True},
    )
    task = resp.json()
    assert len(task["artifacts"]) > 0

    text_parts = [
        p["text"]
        for art in task["artifacts"]
        for p in art["parts"]
        if p.get("kind") == "text"
    ]
    assert FAKE_RESPONSE in text_parts


@pytest.mark.asyncio
async def test_rest_get_task_returns_task(a2a_http, patch_session) -> None:
    """GET /a2a/v1/tasks/{id} returns the created task."""
    create = await a2a_http.post(
        "/a2a/v1/tasks",
        json={"message": user_message_dict(), "blocking": True},
    )
    task_id = create.json()["id"]

    resp = await a2a_http.get(f"/a2a/v1/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == task_id


@pytest.mark.asyncio
async def test_rest_get_task_not_found(a2a_http) -> None:
    """GET /a2a/v1/tasks/{nonexistent} returns 404."""
    resp = await a2a_http.get("/a2a/v1/tasks/no-such-task-id")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rest_list_tasks_empty_initially(a2a_http) -> None:
    """GET /a2a/v1/tasks returns an empty list for an unknown context."""
    ctx = f"ctx-empty-{uuid.uuid4().hex[:8]}"
    resp = await a2a_http.get("/a2a/v1/tasks", params={"contextId": ctx})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tasks"] == []


@pytest.mark.asyncio
async def test_rest_list_tasks_populated(a2a_http, patch_session) -> None:
    """GET /a2a/v1/tasks returns all tasks for a context."""
    ctx = f"ctx-list-{uuid.uuid4().hex[:8]}"
    for _ in range(3):
        await a2a_http.post(
            "/a2a/v1/tasks",
            json={
                "message": user_message_dict(),
                "contextId": ctx,
                "blocking": True,
            },
        )

    resp = await a2a_http.get("/a2a/v1/tasks", params={"contextId": ctx})
    assert resp.status_code == 200
    assert len(resp.json()["tasks"]) == 3


@pytest.mark.asyncio
async def test_rest_list_tasks_filter_by_state(a2a_http, patch_session) -> None:
    """GET /a2a/v1/tasks?state=completed filters to completed tasks only."""
    ctx = f"ctx-state-{uuid.uuid4().hex[:8]}"
    await a2a_http.post(
        "/a2a/v1/tasks",
        json={"message": user_message_dict(), "contextId": ctx, "blocking": True},
    )

    # Filter by completed state
    resp = await a2a_http.get(
        "/a2a/v1/tasks",
        params={"contextId": ctx, "state": A2ATaskState.COMPLETED.value},
    )
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert all(t["status"]["state"] == A2ATaskState.COMPLETED.value for t in tasks)


@pytest.mark.asyncio
async def test_rest_cancel_pending_task(a2a_http, store: InMemoryTaskStore) -> None:
    """POST /a2a/v1/tasks/{id}:cancel cancels a PENDING task."""
    # Create directly in store so the task stays PENDING (no agent run)
    task = await store.create_task(
        context_id="ctx-cancel",
        initial_message=A2AMessage(
            role=A2ARole.USER,
            messageId="m-cancel-rest",
            parts=[TextPart(text="cancel me")],
        ),
    )

    resp = await a2a_http.post(f"/a2a/v1/tasks/{task.id}:cancel")
    assert resp.status_code == 200
    assert resp.json()["status"]["state"] == A2ATaskState.CANCELED.value


@pytest.mark.asyncio
async def test_rest_cancel_completed_task_returns_409(
    a2a_http, patch_session
) -> None:
    """Canceling a COMPLETED task returns HTTP 409 Conflict."""
    create = await a2a_http.post(
        "/a2a/v1/tasks",
        json={"message": user_message_dict(), "blocking": True},
    )
    task_id = create.json()["id"]

    resp = await a2a_http.post(f"/a2a/v1/tasks/{task_id}:cancel")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_rest_task_preserves_context_id(a2a_http, patch_session) -> None:
    """Created task must carry the contextId supplied in the request."""
    ctx = f"ctx-preserve-{uuid.uuid4().hex[:8]}"
    resp = await a2a_http.post(
        "/a2a/v1/tasks",
        json={"message": user_message_dict(), "contextId": ctx, "blocking": True},
    )
    assert resp.json()["contextId"] == ctx


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 transport — /a2a/rpc
# ---------------------------------------------------------------------------


def _rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": "test-1", "method": method, "params": params}


@pytest.mark.asyncio
async def test_jsonrpc_message_send_creates_task(a2a_http, patch_session) -> None:
    """JSON-RPC message/send returns a completed task result."""
    body = _rpc(
        "message/send",
        {
            "message": user_message_dict("hello rpc"),
            "configuration": {"blocking": True},
        },
    )
    resp = await a2a_http.post("/a2a/rpc", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert "result" in data
    assert data["result"]["status"]["state"] == A2ATaskState.COMPLETED.value


@pytest.mark.asyncio
async def test_jsonrpc_tasks_get(a2a_http, patch_session) -> None:
    """JSON-RPC tasks/get retrieves a task by ID."""
    create_body = _rpc(
        "message/send",
        {"message": user_message_dict(), "configuration": {"blocking": True}},
    )
    task_id = (await a2a_http.post("/a2a/rpc", json=create_body)).json()["result"]["id"]

    get_body = _rpc("tasks/get", {"taskId": task_id})
    resp = await a2a_http.post("/a2a/rpc", json=get_body)
    assert resp.status_code == 200
    assert resp.json()["result"]["id"] == task_id


@pytest.mark.asyncio
async def test_jsonrpc_tasks_get_not_found(a2a_http) -> None:
    """JSON-RPC tasks/get with an unknown taskId returns error code -32001."""
    body = _rpc("tasks/get", {"taskId": "ghost-task"})
    resp = await a2a_http.post("/a2a/rpc", json=body)
    assert resp.status_code == 200  # JSON-RPC errors are still HTTP 200
    data = resp.json()
    assert "error" in data
    assert data["error"]["code"] == -32001  # TaskNotFound


@pytest.mark.asyncio
async def test_jsonrpc_tasks_list(a2a_http, patch_session) -> None:
    """JSON-RPC tasks/list returns tasks for a given contextId."""
    ctx = f"ctx-rpc-list-{uuid.uuid4().hex[:8]}"
    for _ in range(2):
        await a2a_http.post(
            "/a2a/rpc",
            json=_rpc(
                "message/send",
                {
                    "message": user_message_dict(),
                    "contextId": ctx,
                    "configuration": {"blocking": True},
                },
            ),
        )

    list_resp = await a2a_http.post(
        "/a2a/rpc",
        json=_rpc("tasks/list", {"contextId": ctx, "limit": 10}),
    )
    assert list_resp.status_code == 200
    result = list_resp.json()["result"]
    assert len(result["tasks"]) == 2


@pytest.mark.asyncio
async def test_jsonrpc_tasks_cancel_pending(a2a_http, store: InMemoryTaskStore) -> None:
    """JSON-RPC tasks/cancel cancels a PENDING task."""
    task = await store.create_task(
        context_id="ctx-rpc-cancel",
        initial_message=A2AMessage(
            role=A2ARole.USER,
            messageId="m-rpc-cancel",
            parts=[TextPart(text="cancel via rpc")],
        ),
    )

    body = _rpc("tasks/cancel", {"taskId": task.id})
    resp = await a2a_http.post("/a2a/rpc", json=body)
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["status"]["state"] == A2ATaskState.CANCELED.value


@pytest.mark.asyncio
async def test_jsonrpc_agent_card_method(a2a_http) -> None:
    """JSON-RPC agent/authenticatedExtendedCard returns the agent card."""
    body = _rpc("agent/authenticatedExtendedCard", {})
    resp = await a2a_http.post("/a2a/rpc", json=body)
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["protocolVersion"] == "0.3"
    assert "capabilities" in result


@pytest.mark.asyncio
async def test_jsonrpc_unknown_method_returns_error(a2a_http) -> None:
    """JSON-RPC unknown method returns error code -32601 (MethodNotFound)."""
    body = _rpc("no/such/method", {})
    resp = await a2a_http.post("/a2a/rpc", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data
    assert data["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_jsonrpc_message_stream_redirects_to_sse(a2a_http) -> None:
    """JSON-RPC message/stream over /a2a/rpc returns an error pointing to the SSE
    endpoint rather than attempting to stream inside a JSON-RPC response."""
    body = _rpc("message/stream", {"message": user_message_dict()})
    resp = await a2a_http.post("/a2a/rpc", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data
    # Error message should mention streaming or the SSE endpoint
    msg = data["error"]["message"].lower()
    assert "sse" in msg or "streaming" in msg
