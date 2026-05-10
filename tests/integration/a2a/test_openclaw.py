"""Integration tests: OpenClaw gateway bridge.

Tests the ``OpenClawBridge`` client and ``BackendRoutingPolicy``
against a lightweight mock FastAPI app served via httpx ASGI transport.
No real Obscura server or LLM backend is needed.

Scenarios:
- BackendRoutingPolicy routing logic (pure unit-level, no HTTP)
- spawn_agent, run_agent, get_agent_status
- store_memory, get_memory, semantic_search
- run_workflow with model routing and retry logic
- Health check endpoint
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request

from obscura.openclaw_bridge import (
    BackendRoutingPolicy,
    MemoryWriteRequest,
    OpenClawBridge,
    OpenClawBridgeConfig,
    RequestMetadata,
    RunAgentRequest,
    SemanticSearchRequest,
    SpawnAgentRequest,
    WorkflowRunRequest,
)

pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Mock OpenClaw API (serves as the Obscura API from the bridge's POV)
# ---------------------------------------------------------------------------


_AGENT_STORE: dict[str, dict[str, Any]] = {}
_KV_STORE: dict[str, Any] = {}
_WORKFLOW_FAIL_UNTIL: int = 0  # set in tests to simulate retries


def _make_mock_openclaw_api() -> FastAPI:
    """Minimal FastAPI app that mirrors the Obscura API surface used by OpenClawBridge."""
    app = FastAPI(title="Mock OpenClaw API")

    @app.post("/api/v1/agents")
    async def spawn_agent(request: Request) -> dict[str, Any]:
        body = await request.json()
        agent_id = f"agent-{uuid.uuid4().hex[:8]}"
        agent = {
            "id": agent_id,
            "name": body.get("name", "unnamed"),
            "model": body.get("model", "claude"),
            "status": "ready",
        }
        _AGENT_STORE[agent_id] = agent
        return agent

    @app.post("/api/v1/agents/{agent_id}/run")
    async def run_agent(agent_id: str, request: Request) -> dict[str, Any]:
        if agent_id not in _AGENT_STORE:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        body = await request.json()
        return {
            "id": agent_id,
            "status": "completed",
            "output": f"ran: {body.get('prompt', '')}",
        }

    @app.get("/api/v1/agents/{agent_id}")
    async def get_agent_status(agent_id: str) -> dict[str, Any]:
        if agent_id not in _AGENT_STORE:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        return _AGENT_STORE[agent_id]

    @app.post("/api/v1/memory/{namespace}/{key}")
    async def store_memory(
        namespace: str, key: str, request: Request
    ) -> dict[str, Any]:
        body = await request.json()
        _KV_STORE[f"{namespace}/{key}"] = body.get("value")
        return {"ok": True}

    @app.get("/api/v1/memory/{namespace}/{key}")
    async def get_memory(namespace: str, key: str) -> dict[str, Any]:
        store_key = f"{namespace}/{key}"
        if store_key not in _KV_STORE:
            raise HTTPException(status_code=404, detail="not found")
        return {"value": _KV_STORE[store_key]}

    @app.get("/api/v1/vector-memory/search")
    async def semantic_search(q: str, top_k: int = 3) -> dict[str, Any]:
        return {
            "results": [
                {"text": f"result for '{q}'", "score": 0.9},
                {"text": f"alternate for '{q}'", "score": 0.7},
            ][:top_k]
        }

    @app.post("/api/v1/workflows/run")
    async def run_workflow(request: Request) -> dict[str, Any]:
        global _WORKFLOW_FAIL_UNTIL
        if _WORKFLOW_FAIL_UNTIL > 0:
            _WORKFLOW_FAIL_UNTIL -= 1
            return app.state  # will be overridden below
        body = await request.json()
        return {
            "status": "completed",
            "task_type": body.get("task_type"),
            "model": body.get("model"),
            "result": "workflow result",
        }

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok"}

    return app


# ---------------------------------------------------------------------------
# Fixture: bridge wired to mock API
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_stores() -> None:
    """Reset in-memory stores between tests."""
    _AGENT_STORE.clear()
    _KV_STORE.clear()


@pytest.fixture()
async def bridge() -> AsyncIterator[OpenClawBridge]:
    """OpenClawBridge pointing at the mock API via ASGI transport."""
    mock_api = _make_mock_openclaw_api()
    transport = httpx.ASGITransport(app=mock_api)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        timeout=30.0,
    )
    b = OpenClawBridge(
        config=OpenClawBridgeConfig(
            base_url="http://testserver",
            workflow_retry_backoff_seconds=0.0,  # no sleep in tests
        ),
        client=client,
    )
    yield b
    await b.aclose()


# ---------------------------------------------------------------------------
# BackendRoutingPolicy — pure routing logic, no HTTP
# ---------------------------------------------------------------------------


def test_routing_policy_known_task_type() -> None:
    """Known task types route to their configured model."""
    policy = BackendRoutingPolicy()
    assert policy.select_model("review") == "claude"
    assert policy.select_model("codegen") == "openai"
    assert policy.select_model("summarize") == "copilot"


def test_routing_policy_unknown_type_uses_default() -> None:
    """Unknown task types fall back to the default model."""
    policy = BackendRoutingPolicy(default_model="claude")
    assert policy.select_model("unknown-task-xyz") == "claude"


def test_routing_policy_empty_type_uses_default() -> None:
    """Empty task type string uses the default model."""
    policy = BackendRoutingPolicy(default_model="copilot")
    assert policy.select_model("") == "copilot"
    assert policy.select_model("  ") == "copilot"


def test_routing_policy_model_candidates_explicit_model() -> None:
    """model_candidates() puts the explicit model first."""
    policy = BackendRoutingPolicy()
    candidates = policy.model_candidates("review", explicit_model="openai")
    assert candidates[0] == "openai"
    # claude is the route for "review" — should still appear in fallback chain
    assert "claude" in candidates


def test_routing_policy_model_candidates_no_duplicates() -> None:
    """model_candidates() never returns the same model twice."""
    policy = BackendRoutingPolicy()
    for task_type in ("review", "codegen", "summarize", ""):
        candidates = policy.model_candidates(task_type, explicit_model=None)
        assert len(candidates) == len(set(candidates)), (
            f"Duplicate models in candidates for task_type={task_type!r}: {candidates}"
        )


def test_routing_policy_fallback_order_included() -> None:
    """All fallback_order models appear in model_candidates()."""
    policy = BackendRoutingPolicy()
    candidates = policy.model_candidates("review", explicit_model=None)
    for model in policy.fallback_order:
        assert model in candidates, f"Fallback model {model!r} missing from candidates"


# ---------------------------------------------------------------------------
# spawn_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_agent_returns_id(bridge: OpenClawBridge) -> None:
    """spawn_agent() returns an agent dict with an id field."""
    req = SpawnAgentRequest(name="molty", model="claude")
    result = await bridge.spawn_agent(req)
    assert "id" in result
    assert result["name"] == "molty"
    assert result["status"] == "ready"


@pytest.mark.asyncio
async def test_spawn_agent_with_metadata(bridge: OpenClawBridge) -> None:
    """spawn_agent() forwards request metadata headers without error."""
    req = SpawnAgentRequest(name="agent-with-meta")
    meta = RequestMetadata(
        correlation_id="corr-abc",
        idempotency_key="idem-xyz",
    )
    result = await bridge.spawn_agent(req, metadata=meta)
    assert "id" in result


# ---------------------------------------------------------------------------
# run_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_executes_prompt(bridge: OpenClawBridge) -> None:
    """run_agent() posts the prompt and returns the output."""
    spawn = await bridge.spawn_agent(SpawnAgentRequest(name="runner"))
    agent_id = spawn["id"]

    result = await bridge.run_agent(
        agent_id, RunAgentRequest(prompt="Summarize this data")
    )
    assert result["status"] == "completed"
    assert "ran: Summarize this data" in result["output"]


# ---------------------------------------------------------------------------
# get_agent_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_agent_status(bridge: OpenClawBridge) -> None:
    """get_agent_status() returns the agent record by ID."""
    spawn = await bridge.spawn_agent(SpawnAgentRequest(name="status-check"))
    agent_id = spawn["id"]

    status = await bridge.get_agent_status(agent_id)
    assert status["id"] == agent_id
    assert status["status"] == "ready"


# ---------------------------------------------------------------------------
# Memory ops
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_and_get_memory(bridge: OpenClawBridge) -> None:
    """store_memory + get_memory round-trip."""
    await bridge.store_memory(
        MemoryWriteRequest(namespace="openclaw", key="widget-count", value=42)
    )
    value = await bridge.get_memory("openclaw", "widget-count")
    assert value == 42


@pytest.mark.asyncio
async def test_get_memory_missing_returns_none(bridge: OpenClawBridge) -> None:
    """get_memory() returns None when the key doesn't exist (404 from server)."""
    result = await bridge.get_memory("openclaw", "missing-key-xyz")
    assert result is None


@pytest.mark.asyncio
async def test_store_memory_string_value(bridge: OpenClawBridge) -> None:
    """Memory store handles string values correctly."""
    await bridge.store_memory(
        MemoryWriteRequest(namespace="ns", key="my-key", value="hello world")
    )
    assert await bridge.get_memory("ns", "my-key") == "hello world"


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_search_returns_results(bridge: OpenClawBridge) -> None:
    """semantic_search() returns a list of result dicts."""
    results = await bridge.semantic_search(
        SemanticSearchRequest(query="agent capabilities", top_k=2)
    )
    assert isinstance(results, list)
    assert len(results) <= 2
    assert all("text" in r for r in results)


# ---------------------------------------------------------------------------
# run_workflow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_workflow_success(bridge: OpenClawBridge) -> None:
    """run_workflow() returns a result dict with telemetry."""
    req = WorkflowRunRequest(
        task_type="review",
        goal="review the PR",
        model="claude",
    )
    result = await bridge.run_workflow(req)
    assert result["status"] == "completed"
    assert "telemetry" in result
    assert len(result["telemetry"]["attempts"]) >= 1


@pytest.mark.asyncio
async def test_run_workflow_routes_to_correct_model(bridge: OpenClawBridge) -> None:
    """run_workflow() uses the routing policy to select the model."""
    req = WorkflowRunRequest(task_type="codegen", goal="write tests")
    result = await bridge.run_workflow(req)
    # codegen routes to openai per BackendRoutingPolicy defaults
    assert result["model"] == "openai"


@pytest.mark.asyncio
async def test_run_workflow_explicit_model_overrides_policy(
    bridge: OpenClawBridge,
) -> None:
    """Explicit model in WorkflowRunRequest overrides the routing policy."""
    req = WorkflowRunRequest(
        task_type="codegen",  # normally routes to openai
        goal="write tests",
        model="claude",  # explicit override
    )
    result = await bridge.run_workflow(req)
    assert result["model"] == "claude"


@pytest.mark.asyncio
async def test_run_workflow_includes_task_type(bridge: OpenClawBridge) -> None:
    """Workflow result echoes the task_type from the request payload."""
    req = WorkflowRunRequest(task_type="summarize", goal="summarize docs")
    result = await bridge.run_workflow(req)
    assert result["task_type"] == "summarize"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check(bridge: OpenClawBridge) -> None:
    """health() returns a dict with status=ok."""
    result = await bridge.health()
    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# Context manager protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_context_manager() -> None:
    """OpenClawBridge can be used as an async context manager."""
    mock_api = _make_mock_openclaw_api()
    transport = httpx.ASGITransport(app=mock_api)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        timeout=10.0,
    )
    async with OpenClawBridge(client=client) as b:
        result = await b.health()
        assert result["status"] == "ok"
