"""Smoke tests for sdk.openclaw_bridge against the FastAPI app."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from obscura.core.config import ObscuraConfig
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


@pytest.mark.asyncio
async def test_openclaw_bridge_smoke_end_to_end() -> None:
    """Bridge should perform basic agent + memory + semantic operations."""
    from obscura.server import create_app

    app = create_app(ObscuraConfig(auth_enabled=False, otel_enabled=False))
    transport = ASGITransport(app=app)

    agent = MagicMock()
    agent.id = "agent-smoke-1"
    agent.config = MagicMock()
    agent.config.name = "smoke-agent"
    agent.status = MagicMock()
    agent.status.name = "RUNNING"
    agent.created_at = datetime.now(UTC)
    agent.run = AsyncMock(return_value="smoke-result")
    agent.start = AsyncMock()

    state = MagicMock()
    state.agent_id = "agent-smoke-1"
    state.name = "smoke-agent"
    state.status = MagicMock()
    state.status.name = "RUNNING"
    state.created_at = datetime.now(UTC)
    state.updated_at = datetime.now(UTC)
    state.iteration_count = 1
    state.error_message = None

    runtime = AsyncMock()
    runtime.spawn = MagicMock(return_value=agent)
    runtime.get_agent = MagicMock(return_value=agent)
    runtime.get_agent_status = MagicMock(return_value=state)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer local-dev-token"},
    ) as client:
        bridge = OpenClawBridge(
            OpenClawBridgeConfig(base_url="http://testserver"),
            client=client,
        )

        with patch("obscura.routes.agents.get_runtime", return_value=runtime):
            spawned = await bridge.spawn_agent(
                SpawnAgentRequest(
                    name="smoke-agent",
                    model="claude",
                    system_prompt="You are a smoke agent.",
                    memory_namespace="openclaw-smoke",
                )
            )
            assert spawned["agent_id"] == "agent-smoke-1"

            run_result = await bridge.run_agent(
                "agent-smoke-1",
                RunAgentRequest(prompt="hello", context={"source": "smoke"}),
            )
            assert run_result["result"] == "smoke-result"

            status = await bridge.get_agent_status("agent-smoke-1")
            assert status["status"] == "RUNNING"

        await bridge.store_memory(
            MemoryWriteRequest(
                namespace="openclaw-smoke",
                key="hello",
                value={"msg": "world"},
            )
        )
        value = await bridge.get_memory("openclaw-smoke", "hello")
        assert value == {"msg": "world"}

        await client.post(
            "/api/v1/vector-memory/openclaw-smoke/doc-1",
            json={"text": "OpenClaw bridge smoke document", "memory_type": "note"},
        )
        results = await bridge.semantic_search(
            SemanticSearchRequest(query="smoke document", namespace="openclaw-smoke")
        )
        assert isinstance(results, list)

        health = await bridge.health()
        assert health.get("status") == "ok"


@pytest.mark.asyncio
async def test_openclaw_bridge_run_workflow_uses_routing_policy() -> None:
    from obscura.server import create_app

    app = create_app(ObscuraConfig(auth_enabled=False, otel_enabled=False))
    transport = ASGITransport(app=app)

    agent = MagicMock()
    agent.id = "agent-smoke-2"
    agent.config = MagicMock()
    agent.config.name = "workflow-agent"
    agent.status = MagicMock()
    agent.status.name = "RUNNING"
    agent.created_at = datetime.now(UTC)
    agent.run = AsyncMock(return_value="workflow-result")
    agent.start = AsyncMock()
    agent.stop = AsyncMock()

    runtime = AsyncMock()
    runtime.spawn = MagicMock(return_value=agent)

    policy = BackendRoutingPolicy(default_model="claude", routes={"codegen": "openai"})

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer local-dev-token"},
    ) as client:
        bridge = OpenClawBridge(
            OpenClawBridgeConfig(base_url="http://testserver"),
            routing_policy=policy,
            client=client,
        )
        with patch("obscura.routes.workflows.get_runtime", return_value=runtime):
            result = await bridge.run_workflow(
                WorkflowRunRequest(
                    task_type="codegen",
                    goal="Write a tiny function.",
                    context={"language": "python"},
                )
            )
        assert result["status"] == "completed"
        assert result["model"] == "openai"
        telemetry_attempts = result["telemetry"]["attempts"]
        assert telemetry_attempts[0]["attempt"] == 1
        assert telemetry_attempts[0]["model"] == "openai"
        assert telemetry_attempts[0]["retry_reason"] == ""
        runtime.spawn.assert_called_once()
        assert runtime.spawn.call_args.kwargs["model"] == "openai"


@pytest.mark.asyncio
async def test_openclaw_bridge_sets_request_and_idempotency_headers() -> None:
    seen_headers: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_headers["x-request-id"] = request.headers.get("x-request-id", "")
        seen_headers["x-idempotency-key"] = request.headers.get("x-idempotency-key", "")
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer local-dev-token"},
    ) as client:
        bridge = OpenClawBridge(
            OpenClawBridgeConfig(base_url="http://testserver"),
            client=client,
        )
        await bridge.health(
            RequestMetadata(correlation_id="req-123", idempotency_key="idem-123")
        )

    assert seen_headers["x-request-id"] == "req-123"
    assert seen_headers["x-idempotency-key"] == "idem-123"


@pytest.mark.asyncio
async def test_openclaw_bridge_run_agent_sends_timeout_and_cancellation() -> None:
    seen_payload: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/run"):
            payload = cast(
                dict[str, Any],
                json.loads(request.content.decode("utf-8")),
            )
            seen_payload.update(payload)
            return httpx.Response(
                200, json={"agent_id": "a1", "status": "RUNNING", "result": "ok"}
            )
        return httpx.Response(404, json={"detail": "not found"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer local-dev-token"},
    ) as client:
        bridge = OpenClawBridge(
            OpenClawBridgeConfig(base_url="http://testserver"),
            client=client,
        )
        result = await bridge.run_agent(
            "a1",
            RunAgentRequest(
                prompt="hello",
                context={"source": "smoke"},
                timeout_seconds=9.5,
                cancellation_token="cancel-123",
            ),
        )
        assert result["result"] == "ok"

    assert seen_payload["timeout_seconds"] == 9.5
    assert seen_payload["cancellation_token"] == "cancel-123"


@pytest.mark.asyncio
async def test_openclaw_bridge_workflow_retries_then_succeeds() -> None:
    attempts: list[str] = []
    call_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        payload = json.loads(request.content.decode("utf-8"))
        attempts.append(str(payload.get("model")))
        call_count += 1
        if call_count == 1:
            return httpx.Response(503, json={"detail": "temporary"})
        return httpx.Response(
            200,
            json={"status": "completed", "model": payload.get("model"), "result": "ok"},
        )

    transport = httpx.MockTransport(handler)
    config = OpenClawBridgeConfig(
        base_url="http://testserver",
        workflow_max_retries=2,
        workflow_retry_backoff_seconds=0.0,
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer local-dev-token"},
    ) as client:
        bridge = OpenClawBridge(config, client=client)
        result = await bridge.run_workflow(
            WorkflowRunRequest(task_type="codegen", goal="write code", model="openai")
        )

    assert result["status"] == "completed"
    assert attempts == ["openai", "openai"]
    telemetry_attempts = result["telemetry"]["attempts"]
    assert telemetry_attempts[0]["attempt"] == 1
    assert telemetry_attempts[0]["model"] == "openai"
    assert telemetry_attempts[0]["retry_reason"] == "http_503"
    assert telemetry_attempts[1]["attempt"] == 2
    assert telemetry_attempts[1]["model"] == "openai"
    assert telemetry_attempts[1]["retry_reason"] == ""


@pytest.mark.asyncio
async def test_openclaw_bridge_workflow_fallback_to_next_model() -> None:
    attempts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        model = str(payload.get("model"))
        attempts.append(model)
        if model == "openai":
            return httpx.Response(500, json={"detail": "backend failed"})
        return httpx.Response(
            200,
            json={"status": "completed", "model": model, "result": "ok"},
        )

    transport = httpx.MockTransport(handler)
    policy = BackendRoutingPolicy(
        default_model="openai",
        routes={"codegen": "openai"},
        fallback_order=("claude",),
    )
    config = OpenClawBridgeConfig(
        base_url="http://testserver",
        workflow_max_retries=0,
        workflow_retry_backoff_seconds=0.0,
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer local-dev-token"},
    ) as client:
        bridge = OpenClawBridge(config, routing_policy=policy, client=client)
        result = await bridge.run_workflow(
            WorkflowRunRequest(task_type="codegen", goal="write code")
        )

    assert result["status"] == "completed"
    assert result["model"] == "claude"
    assert attempts == ["openai", "claude"]
    telemetry_attempts = result["telemetry"]["attempts"]
    assert telemetry_attempts[0]["attempt"] == 1
    assert telemetry_attempts[0]["model"] == "openai"
    assert telemetry_attempts[0]["retry_reason"] == "http_500"
    assert telemetry_attempts[1]["attempt"] == 1
    assert telemetry_attempts[1]["model"] == "claude"
    assert telemetry_attempts[1]["retry_reason"] == ""
