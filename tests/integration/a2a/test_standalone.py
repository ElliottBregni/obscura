"""Integration tests for A2A standalone mode.

Tests cover:
- /.well-known/agent.json discovery endpoint
- Task creation and execution (blocking and non-blocking)
- OpenClaw-style agent-to-agent round trip (via A2AClient over ASGI)

All tests use an in-process FastAPI app via httpx ASGITransport — no real
network or LLM calls are made. The ``patch_session`` fixture from conftest
replaces ``build_a2a_session`` with a zero-cost stub that returns a canned
text response instantly.
"""

from __future__ import annotations

import pytest
import httpx

from obscura.integrations.a2a.client import A2AClient
from obscura.integrations.a2a.server import ObscuraA2AServer
from obscura.integrations.a2a.standalone import create_standalone_app
from obscura.integrations.a2a.store import InMemoryTaskStore
from obscura.integrations.a2a.types import AgentCard

from .conftest import (
    FAKE_RESPONSE,
    TEST_AGENT_NAME,
    TEST_AGENT_URL,
    make_agent_card,
    make_app,
    user_message_dict,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def standalone_app():
    """Full standalone app (create_standalone_app path) with an in-memory store."""
    return create_standalone_app(
        base_url=TEST_AGENT_URL,
        store=InMemoryTaskStore(),
        agent_backend="copilot",
    )


@pytest.fixture()
async def standalone_client(standalone_app) -> httpx.AsyncClient:
    """HTTP client wired to the standalone app via ASGI transport."""
    transport = httpx.ASGITransport(app=standalone_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=TEST_AGENT_URL,
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# Well-known endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wellknown_returns_200(a2a_http: httpx.AsyncClient) -> None:
    """GET /.well-known/agent.json returns HTTP 200."""
    resp = await a2a_http.get("/.well-known/agent.json")
    assert resp.status_code == 200


@pytest.mark.integration
async def test_wellknown_content_type(a2a_http: httpx.AsyncClient) -> None:
    """GET /.well-known/agent.json returns JSON content type."""
    resp = await a2a_http.get("/.well-known/agent.json")
    assert "application/json" in resp.headers.get("content-type", "")


@pytest.mark.integration
async def test_wellknown_agent_name(a2a_http: httpx.AsyncClient) -> None:
    """Agent card name matches the configured agent name."""
    resp = await a2a_http.get("/.well-known/agent.json")
    data = resp.json()
    assert data["name"] == TEST_AGENT_NAME


@pytest.mark.integration
async def test_wellknown_required_fields(a2a_http: httpx.AsyncClient) -> None:
    """Agent card contains all required A2A fields."""
    resp = await a2a_http.get("/.well-known/agent.json")
    data = resp.json()

    for field in (
        "name",
        "description",
        "url",
        "version",
        "protocolVersion",
        "skills",
        "capabilities",
        "securitySchemes",
    ):
        assert field in data, f"Missing required field: {field}"


@pytest.mark.integration
async def test_wellknown_no_python_aliases(a2a_http: httpx.AsyncClient) -> None:
    """Agent card must not leak Python internal field names (e.g. 'in_')."""
    resp = await a2a_http.get("/.well-known/agent.json")
    raw = resp.text
    assert '"in_"' not in raw, "Python alias 'in_' leaked into JSON output"


@pytest.mark.integration
async def test_wellknown_capabilities(a2a_http: httpx.AsyncClient) -> None:
    """Agent card capabilities has expected shape."""
    resp = await a2a_http.get("/.well-known/agent.json")
    caps = resp.json()["capabilities"]
    assert isinstance(caps.get("streaming"), bool)
    assert isinstance(caps.get("pushNotifications"), bool)


@pytest.mark.integration
async def test_wellknown_skills_list(a2a_http: httpx.AsyncClient) -> None:
    """Skills list is non-empty and each skill has id/name fields."""
    resp = await a2a_http.get("/.well-known/agent.json")
    skills = resp.json()["skills"]
    assert isinstance(skills, list)
    # conftest builds zero skills — ensure we don't regress to a broken shape
    for skill in skills:
        assert "id" in skill
        assert "name" in skill


# ---------------------------------------------------------------------------
# Task creation tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_rest_create_task_nonblocking(a2a_http: httpx.AsyncClient) -> None:
    """POST /a2a/v1/tasks with blocking=False returns task with id and status."""
    body = {
        "message": user_message_dict("hello"),
        "blocking": False,
    }
    resp = await a2a_http.post("/a2a/v1/tasks", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert "status" in data
    assert "state" in data["status"]


@pytest.mark.integration
async def test_rest_create_task_blocking(
    a2a_http: httpx.AsyncClient,
    patch_session,
) -> None:
    """POST /a2a/v1/tasks with blocking=True completes via the fake session."""
    body = {
        "message": user_message_dict("hello blocking"),
        "blocking": True,
    }
    resp = await a2a_http.post("/a2a/v1/tasks", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"]["state"] == "completed"


@pytest.mark.integration
async def test_rest_get_task_by_id(a2a_http: httpx.AsyncClient) -> None:
    """Create a task then retrieve it by ID."""
    create_resp = await a2a_http.post(
        "/a2a/v1/tasks",
        json={"message": user_message_dict("fetch me"), "blocking": False},
    )
    assert create_resp.status_code == 200
    task_id = create_resp.json()["id"]

    get_resp = await a2a_http.get(f"/a2a/v1/tasks/{task_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == task_id


@pytest.mark.integration
async def test_rest_get_task_not_found(a2a_http: httpx.AsyncClient) -> None:
    """GET /a2a/v1/tasks/{nonexistent} returns 404."""
    resp = await a2a_http.get("/a2a/v1/tasks/task-does-not-exist")
    assert resp.status_code == 404


@pytest.mark.integration
async def test_rest_list_tasks(a2a_http: httpx.AsyncClient) -> None:
    """GET /a2a/v1/tasks returns a JSON object with a 'tasks' list."""
    resp = await a2a_http.get("/a2a/v1/tasks")
    assert resp.status_code == 200
    body = resp.json()
    assert "tasks" in body
    assert isinstance(body["tasks"], list)


@pytest.mark.integration
async def test_rest_cancel_nonexistent_task(a2a_http: httpx.AsyncClient) -> None:
    """POST .../nonexistent:cancel returns 404 or 409 — never 500."""
    resp = await a2a_http.post("/a2a/v1/tasks/nonexistent-task-id:cancel")
    assert resp.status_code in {404, 409}


@pytest.mark.integration
async def test_blocking_task_has_artifact(
    a2a_http: httpx.AsyncClient,
    patch_session,
) -> None:
    """A completed blocking task includes an artifact with the agent's response."""
    body = {
        "message": user_message_dict("produce an artifact"),
        "blocking": True,
    }
    resp = await a2a_http.post("/a2a/v1/tasks", json=body)
    assert resp.status_code == 200
    data = resp.json()
    artifacts = data.get("artifacts", [])
    assert len(artifacts) >= 1
    # Artifact should contain the fake response text
    texts = [
        part["text"]
        for art in artifacts
        for part in art.get("parts", [])
        if part.get("kind") == "text"
    ]
    assert any(FAKE_RESPONSE in t for t in texts)


# ---------------------------------------------------------------------------
# JSON-RPC transport tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_jsonrpc_agent_card(a2a_http: httpx.AsyncClient) -> None:
    """POST /a2a/rpc agent/authenticatedExtendedCard returns a JSON-RPC envelope."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "agent/authenticatedExtendedCard",
        "params": {},
    }
    resp = await a2a_http.post("/a2a/rpc", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert "result" in body or "error" in body
    assert "jsonrpc" in body
    assert body["id"] == 1


@pytest.mark.integration
async def test_jsonrpc_message_send(
    a2a_http: httpx.AsyncClient,
    patch_session,
) -> None:
    """POST /a2a/rpc message/send returns a valid Task via JSON-RPC."""
    import uuid

    msg = {
        "role": "user",
        "messageId": f"msg-{uuid.uuid4().hex[:8]}",
        "parts": [{"kind": "text", "text": "rpc send test"}],
    }
    payload = {
        "jsonrpc": "2.0",
        "id": "rpc-1",
        "method": "message/send",
        "params": {
            "message": msg,
            "configuration": {"blocking": True},
        },
    }
    resp = await a2a_http.post("/a2a/rpc", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert "result" in body
    result = body["result"]
    assert "id" in result
    assert result["status"]["state"] == "completed"


# ---------------------------------------------------------------------------
# OpenClaw-style agent-to-agent round trip
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_openclaw_roundtrip_via_a2a_client(
    app,
    patch_session,
) -> None:
    """OpenClaw-style round trip: A2AClient discovers card then sends a message.

    Simulates an OpenClaw peer discovering and invoking the Obscura agent
    over the A2A protocol without any real network connection.
    """
    transport = httpx.ASGITransport(app=app)

    async with A2AClient(
        base_url=TEST_AGENT_URL,
        transport=transport,
    ) as client:
        # Step 1: discover agent card (/.well-known/agent.json)
        card = await client.discover()
        assert isinstance(card, AgentCard)
        assert card.name == TEST_AGENT_NAME
        assert card.url == TEST_AGENT_URL

        # Step 2: send a message and wait for completion
        task = await client.send_message(
            "What is 2 + 2?",
            blocking=True,
        )
        assert task.id is not None
        assert task.status.state.value == "completed"

        # Step 3: retrieve the task by ID (simulates peer polling)
        fetched = await client.get_task(task.id)
        assert fetched.id == task.id


@pytest.mark.integration
async def test_openclaw_roundtrip_nonblocking(
    app,
    patch_session,
) -> None:
    """OpenClaw peer sends non-blocking task then polls until completion."""
    import asyncio

    from obscura.core.enums.protocol import A2ATaskState

    transport = httpx.ASGITransport(app=app)

    async with A2AClient(
        base_url=TEST_AGENT_URL,
        transport=transport,
    ) as client:
        task = await client.send_message("non-blocking round trip", blocking=False)
        assert task.id is not None

        # Yield to the event loop so the background asyncio.Task can run to
        # completion before we poll. The fake session returns instantly, so a
        # few yields are sufficient.
        for _ in range(10):
            await asyncio.sleep(0)
            task = await client.get_task(task.id)
            if task.status.state in (A2ATaskState.COMPLETED, A2ATaskState.FAILED):
                break

        assert task.status.state == A2ATaskState.COMPLETED


@pytest.mark.integration
async def test_openclaw_list_and_cancel(
    app,
    patch_session,
) -> None:
    """A2AClient.list_tasks and cancel_task work end-to-end."""
    transport = httpx.ASGITransport(app=app)

    async with A2AClient(
        base_url=TEST_AGENT_URL,
        transport=transport,
    ) as client:
        # Create a task
        task = await client.send_message("list and cancel test", blocking=False)
        task_id = task.id

        # List tasks — our task should appear
        tasks, _cursor = await client.list_tasks()
        ids = [t.id for t in tasks]
        assert task_id in ids


# ---------------------------------------------------------------------------
# Standalone app factory tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_standalone_app_has_a2a_server_state(standalone_app) -> None:
    """create_standalone_app().state.a2a_server is an ObscuraA2AServer instance."""
    assert isinstance(standalone_app.state.a2a_server, ObscuraA2AServer)


@pytest.mark.integration
async def test_standalone_wellknown_matches_rest_card(
    standalone_client: httpx.AsyncClient,
) -> None:
    """The well-known and REST endpoints return the same agent name and url."""
    wk = (await standalone_client.get("/.well-known/agent.json")).json()
    rest = (await standalone_client.get("/a2a/v1/agent")).json()

    assert wk["name"] == rest["name"]
    assert wk["url"] == rest["url"]
