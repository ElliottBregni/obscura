"""End-to-end tests for the standalone A2A FastAPI application.

Spins up the standalone app in-process using ``httpx.AsyncClient`` with
``ASGITransport`` — no real network or LLM backend required.

Coverage:
- Discovery: /.well-known/agent.json shape, serialization correctness
- REST task lifecycle: create → get → list → cancel
- REST error paths: 404 on unknown task, 409/404 on bad cancel
- Context ID grouping: tasks created in the same context appear in list
- JSON-RPC 2.0: message/send, tasks/get, tasks/list, tasks/cancel,
  agent/authenticatedExtendedCard, invalid method error
- SSE streaming: endpoint exists, response is event-stream content-type
- Custom definition: standalone app built from a custom AgentDefinition
- CORS: Access-Control-Allow-Origin header is present on responses
- App state: ObscuraA2AServer is wired onto app.state

Run with::

    pytest tests/e2e/test_a2a_standalone.py -v -m e2e
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from obscura.integrations.a2a.definition import (
    AgentDefinition,
    SkillDefinition,
    openclaw_compatible_definition,
)
from obscura.integrations.a2a.server import ObscuraA2AServer
from obscura.integrations.a2a.standalone import create_standalone_app
from obscura.integrations.a2a.store import InMemoryTaskStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(text: str = "hello", *, mid: str | None = None) -> dict:
    return {
        "role": "user",
        "messageId": mid or f"m-{uuid.uuid4().hex[:8]}",
        "parts": [{"kind": "text", "text": text}],
    }


def _rpc(method: str, params: dict | None = None, *, rpc_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def standalone_app():
    """Session-scoped FastAPI app backed by a fresh in-memory task store."""
    return create_standalone_app(
        base_url="http://testserver",
        store=InMemoryTaskStore(),
        agent_backend="copilot",
    )


@pytest.fixture
async def client(standalone_app):
    """Function-scoped async HTTP client wired to the standalone app."""
    async with httpx.AsyncClient(
        base_url="http://testserver",
        transport=httpx.ASGITransport(app=standalone_app),
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_wellknown_agent_json(client: httpx.AsyncClient) -> None:
    """GET /.well-known/agent.json returns a well-formed A2A agent card."""
    resp = await client.get("/.well-known/agent.json")
    assert resp.status_code == 200

    data = resp.json()
    assert data["name"] == "Obscura Agent"
    assert "url" in data
    assert "protocolVersion" in data
    assert isinstance(data.get("skills"), list)
    assert len(data["skills"]) >= 1
    assert isinstance(data.get("capabilities"), dict)
    assert data["capabilities"]["streaming"] is True


@pytest.mark.e2e
async def test_wellknown_card_serialization(client: httpx.AsyncClient) -> None:
    """Agent card must use JSON alias names and omit null fields."""
    resp = await client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    raw = resp.text

    # Python alias 'in_' must never appear — should be serialised as 'in'
    assert '"in_"' not in raw
    # Null-valued optional fields must be dropped entirely
    assert '"extensions": null' not in raw
    assert '"extensions":null' not in raw

    data = resp.json()
    assert "securitySchemes" in data
    assert "bearer" in data["securitySchemes"]


@pytest.mark.e2e
async def test_wellknown_skills_have_required_fields(client: httpx.AsyncClient) -> None:
    """Every skill in the card has id, name, description, and tags fields."""
    data = (await client.get("/.well-known/agent.json")).json()
    for skill in data["skills"]:
        assert "id" in skill, f"Skill missing id: {skill}"
        assert "name" in skill, f"Skill missing name: {skill}"
        assert "description" in skill, f"Skill missing description: {skill}"
        assert isinstance(skill.get("tags"), list), f"Skill tags not a list: {skill}"


@pytest.mark.e2e
async def test_wellknown_matches_rest_card(client: httpx.AsyncClient) -> None:
    """/.well-known/agent.json and GET /a2a/v1/agent return the same card."""
    wk = (await client.get("/.well-known/agent.json")).json()
    rest = (await client.get("/a2a/v1/agent")).json()
    assert wk["name"] == rest["name"]
    assert wk["url"] == rest["url"]
    assert wk["protocolVersion"] == rest["protocolVersion"]
    assert len(wk["skills"]) == len(rest["skills"])


# ---------------------------------------------------------------------------
# REST — agent card
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_rest_agent_card(client: httpx.AsyncClient) -> None:
    """GET /a2a/v1/agent returns a valid agent card."""
    resp = await client.get("/a2a/v1/agent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Obscura Agent"
    assert "securitySchemes" in data


# ---------------------------------------------------------------------------
# REST — task lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_rest_create_task_nonblocking(client: httpx.AsyncClient) -> None:
    """POST /a2a/v1/tasks (blocking=False) returns a pending task immediately."""
    resp = await client.post(
        "/a2a/v1/tasks", json={"message": _msg(), "blocking": False}
    )
    assert resp.status_code == 200

    data = resp.json()
    assert "id" in data
    assert "status" in data
    assert "state" in data["status"]
    assert "history" in data
    assert len(data["history"]) == 1  # the user message


@pytest.mark.e2e
async def test_rest_task_id_is_stable(client: httpx.AsyncClient) -> None:
    """Task ID returned at creation matches the ID returned by GET."""
    create = (
        await client.post("/a2a/v1/tasks", json={"message": _msg(), "blocking": False})
    ).json()
    task_id = create["id"]

    get = (await client.get(f"/a2a/v1/tasks/{task_id}")).json()
    assert get["id"] == task_id


@pytest.mark.e2e
async def test_rest_get_task_not_found(client: httpx.AsyncClient) -> None:
    """GET /a2a/v1/tasks/{nonexistent} returns 404."""
    resp = await client.get("/a2a/v1/tasks/no-such-task-xyz")
    assert resp.status_code == 404


@pytest.mark.e2e
async def test_rest_list_tasks_returns_list(client: httpx.AsyncClient) -> None:
    """GET /a2a/v1/tasks returns a dict with a tasks list."""
    resp = await client.get("/a2a/v1/tasks")
    assert resp.status_code == 200
    body = resp.json()
    assert "tasks" in body
    assert isinstance(body["tasks"], list)


@pytest.mark.e2e
async def test_rest_list_tasks_by_context(client: httpx.AsyncClient) -> None:
    """Tasks created with a contextId appear when listing by that context."""
    ctx = f"ctx-{uuid.uuid4().hex[:8]}"
    for i in range(3):
        await client.post(
            "/a2a/v1/tasks",
            json={"message": _msg(f"task {i}"), "contextId": ctx, "blocking": False},
        )

    resp = await client.get(f"/a2a/v1/tasks?contextId={ctx}")
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert len(tasks) == 3
    for t in tasks:
        assert t["contextId"] == ctx


@pytest.mark.e2e
async def test_rest_cancel_pending_task(client: httpx.AsyncClient) -> None:
    """POST /a2a/v1/tasks/{id}:cancel on a pending task succeeds."""
    task_id = (
        await client.post("/a2a/v1/tasks", json={"message": _msg(), "blocking": False})
    ).json()["id"]

    cancel_resp = await client.post(f"/a2a/v1/tasks/{task_id}:cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"]["state"] == "canceled"


@pytest.mark.e2e
async def test_rest_cancel_nonexistent_task(client: httpx.AsyncClient) -> None:
    """POST /a2a/v1/tasks/nonexistent:cancel returns 404 or 409."""
    resp = await client.post("/a2a/v1/tasks/no-such-task:cancel")
    assert resp.status_code in {404, 409}


@pytest.mark.e2e
async def test_rest_canceled_task_not_cancelable_again(
    client: httpx.AsyncClient,
) -> None:
    """Canceling an already-canceled task returns 409 (InvalidTransition)."""
    task_id = (
        await client.post("/a2a/v1/tasks", json={"message": _msg(), "blocking": False})
    ).json()["id"]

    await client.post(f"/a2a/v1/tasks/{task_id}:cancel")
    resp = await client.post(f"/a2a/v1/tasks/{task_id}:cancel")
    assert resp.status_code == 409


@pytest.mark.e2e
async def test_rest_task_history_contains_user_message(
    client: httpx.AsyncClient,
) -> None:
    """The created task's history includes the original user message."""
    text = "e2e history check"
    task = (
        await client.post(
            "/a2a/v1/tasks",
            json={"message": _msg(text), "blocking": False},
        )
    ).json()

    assert task["history"][0]["role"] == "user"
    assert task["history"][0]["parts"][0]["text"] == text


# ---------------------------------------------------------------------------
# JSON-RPC 2.0
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_jsonrpc_agent_card(client: httpx.AsyncClient) -> None:
    """JSON-RPC agent/authenticatedExtendedCard returns a result with name field."""
    resp = await client.post("/a2a/rpc", json=_rpc("agent/authenticatedExtendedCard"))
    assert resp.status_code == 200

    body = resp.json()
    assert "result" in body
    assert body["result"]["name"] == "Obscura Agent"


@pytest.mark.e2e
async def test_jsonrpc_message_send(client: httpx.AsyncClient) -> None:
    """JSON-RPC message/send creates a task and returns it in result."""
    payload = _rpc(
        "message/send",
        params={
            "message": _msg("jsonrpc hello"),
            "configuration": {"blocking": False},
        },
    )
    resp = await client.post("/a2a/rpc", json=payload)
    assert resp.status_code == 200

    body = resp.json()
    assert "result" in body
    assert "id" in body["result"]
    assert "status" in body["result"]


@pytest.mark.e2e
async def test_jsonrpc_tasks_get(client: httpx.AsyncClient) -> None:
    """JSON-RPC tasks/get retrieves a task created via REST."""
    task_id = (
        await client.post("/a2a/v1/tasks", json={"message": _msg(), "blocking": False})
    ).json()["id"]

    resp = await client.post("/a2a/rpc", json=_rpc("tasks/get", {"taskId": task_id}))
    assert resp.status_code == 200

    body = resp.json()
    assert "result" in body
    assert body["result"]["id"] == task_id


@pytest.mark.e2e
async def test_jsonrpc_tasks_list(client: httpx.AsyncClient) -> None:
    """JSON-RPC tasks/list returns a result with a tasks array."""
    resp = await client.post("/a2a/rpc", json=_rpc("tasks/list", {}))
    assert resp.status_code == 200

    body = resp.json()
    assert "result" in body
    assert "tasks" in body["result"]
    assert isinstance(body["result"]["tasks"], list)


@pytest.mark.e2e
async def test_jsonrpc_tasks_cancel(client: httpx.AsyncClient) -> None:
    """JSON-RPC tasks/cancel cancels a pending task."""
    task_id = (
        await client.post("/a2a/v1/tasks", json={"message": _msg(), "blocking": False})
    ).json()["id"]

    resp = await client.post("/a2a/rpc", json=_rpc("tasks/cancel", {"taskId": task_id}))
    assert resp.status_code == 200

    body = resp.json()
    assert "result" in body
    assert body["result"]["status"]["state"] == "canceled"


@pytest.mark.e2e
async def test_jsonrpc_invalid_method(client: httpx.AsyncClient) -> None:
    """JSON-RPC with an unknown method returns a -32601 error response."""
    resp = await client.post("/a2a/rpc", json=_rpc("no/such/method"))
    assert resp.status_code == 200  # JSON-RPC errors still return HTTP 200

    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == -32601


@pytest.mark.e2e
async def test_jsonrpc_envelope_ids_match(client: httpx.AsyncClient) -> None:
    """JSON-RPC response id matches the request id."""
    payload = _rpc("agent/authenticatedExtendedCard", rpc_id=42)
    body = (await client.post("/a2a/rpc", json=payload)).json()
    assert body["id"] == 42


# ---------------------------------------------------------------------------
# SSE streaming
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_sse_streaming_endpoint_exists(client: httpx.AsyncClient) -> None:
    """POST /a2a/v1/tasks/streaming responds with text/event-stream content-type."""
    async with client.stream(
        "POST",
        "/a2a/v1/tasks/streaming",
        json={"message": _msg("stream me")},
    ) as resp:
        assert resp.status_code == 200
        ct = resp.headers.get("content-type", "")
        assert "text/event-stream" in ct


@pytest.mark.e2e
async def test_sse_streaming_emits_events(client: httpx.AsyncClient) -> None:
    """POST /a2a/v1/tasks/streaming emits at least one SSE data line."""
    lines: list[str] = []
    async with client.stream(
        "POST",
        "/a2a/v1/tasks/streaming",
        json={"message": _msg("stream events")},
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                lines.append(line)
            if len(lines) >= 1:
                break  # got at least one event — don't block forever

    assert len(lines) >= 1, "SSE stream emitted no data events"


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_cors_header_on_agent_card(client: httpx.AsyncClient) -> None:
    """Standalone app returns CORS header on agent card responses."""
    resp = await client.get(
        "/.well-known/agent.json",
        headers={"Origin": "http://other-origin.example.com"},
    )
    assert resp.status_code == 200
    assert "access-control-allow-origin" in resp.headers


# ---------------------------------------------------------------------------
# Custom definition
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_custom_definition_reflected_in_card() -> None:
    """create_standalone_app with a custom AgentDefinition serves that card."""
    custom = AgentDefinition(
        name="My Custom Agent",
        description="Built for testing",
        skills=[
            SkillDefinition(id="custom-skill", name="Custom", tags=["test"]),
        ],
    )
    app = create_standalone_app(
        definition=custom,
        base_url="http://testserver",
        store=InMemoryTaskStore(),
    )
    async with httpx.AsyncClient(
        base_url="http://testserver",
        transport=httpx.ASGITransport(app=app),
    ) as c:
        data = (await c.get("/.well-known/agent.json")).json()

    assert data["name"] == "My Custom Agent"
    assert len(data["skills"]) == 1
    assert data["skills"][0]["id"] == "custom-skill"


@pytest.mark.e2e
async def test_openclaw_definition_card_has_openclaw_tags() -> None:
    """Standalone app built from openclaw_compatible_definition() has openclaw skill tags."""
    app = create_standalone_app(
        definition=openclaw_compatible_definition(base_url="http://testserver"),
        base_url="http://testserver",
        store=InMemoryTaskStore(),
    )
    async with httpx.AsyncClient(
        base_url="http://testserver",
        transport=httpx.ASGITransport(app=app),
    ) as c:
        data = (await c.get("/.well-known/agent.json")).json()

    for skill in data["skills"]:
        assert "openclaw" in skill["tags"], (
            f"Skill {skill['id']!r} missing openclaw tag"
        )


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_standalone_app_has_a2a_server_state(standalone_app) -> None:
    """app.state.a2a_server is an ObscuraA2AServer instance."""
    assert isinstance(standalone_app.state.a2a_server, ObscuraA2AServer)


@pytest.mark.e2e
def test_standalone_app_title_matches_definition(standalone_app) -> None:
    """FastAPI app title matches the agent definition name."""
    assert standalone_app.title == "Obscura Agent"
