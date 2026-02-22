"""Tests for obscura.routes.observe endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from starlette.testclient import TestClient

from obscura.approvals import clear_tool_approvals, create_tool_approval_request
from obscura.auth.models import AuthenticatedUser
from obscura.core.config import ObscuraConfig
from obscura.memory import MemoryStore


def _anonymous_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="anonymous",
        email="anonymous@obscura.local",
        roles=("admin",),
        org_id="local",
        token_type="anonymous",
        raw_token="",
    )


@pytest.fixture
def app() -> Any:
    config = ObscuraConfig(auth_enabled=False, otel_enabled=False)
    from obscura.server import create_app

    return create_app(config)


@pytest.fixture
def client(app: Any) -> TestClient:
    return TestClient(app)


def test_observe_snapshot_returns_states_and_stale_ids(client: TestClient) -> None:
    import asyncio

    MemoryStore.reset_instances()
    asyncio.run(clear_tool_approvals())
    user = _anonymous_user()
    store = MemoryStore.for_user(user)
    now = datetime.now(UTC)
    store.set(
        "agent_state_agent-1",
        {
            "agent_id": "agent-1",
            "name": "builder-agent",
            "status": "RUNNING",
            "updated_at": (now - timedelta(seconds=40)).isoformat(),
            "iteration_count": 3,
            "error_message": None,
        },
        namespace="agent:runtime",
    )
    store.set(
        "agent_state_agent-2",
        {
            "agent_id": "agent-2",
            "name": "helper-agent",
            "status": "COMPLETED",
            "updated_at": now.isoformat(),
            "iteration_count": 5,
            "error_message": None,
        },
        namespace="agent:runtime",
    )
    asyncio.run(
        create_tool_approval_request(
            user_id="anonymous",
            agent_id="agent-1",
            tool_use_id="tool-use-1",
            tool_name="run_shell",
            tool_input={"script": "pwd"},
        )
    )

    resp = client.get("/api/v1/observe", params={"stale_seconds": 20})
    assert resp.status_code == 200
    data = resp.json()
    assert data["namespace"] == "agent:runtime"
    assert data["count"] == 2
    assert data["stale_agent_ids"] == ["agent-1"]
    assert len(data["pending_tool_approvals"]) == 1
    assert data["pending_tool_approvals"][0]["tool_name"] == "run_shell"
    ids = {entry["agent_id"] for entry in data["states"]}
    assert ids == {"agent-1", "agent-2"}


def test_observe_stream_once_emits_snapshot_event(client: TestClient) -> None:
    import asyncio

    MemoryStore.reset_instances()
    asyncio.run(clear_tool_approvals())
    user = _anonymous_user()
    store = MemoryStore.for_user(user)
    now = datetime.now(UTC)
    store.set(
        "agent_state_agent-xyz",
        {
            "agent_id": "agent-xyz",
            "name": "watch-agent",
            "status": "WAITING",
            "updated_at": now.isoformat(),
            "iteration_count": 1,
            "error_message": None,
        },
        namespace="agent:runtime",
    )
    asyncio.run(
        create_tool_approval_request(
            user_id="anonymous",
            agent_id="agent-xyz",
            tool_use_id="tool-use-xyz",
            tool_name="run_python3",
            tool_input={"script": "print(1)"},
        )
    )

    with client.stream("GET", "/api/v1/observe/stream?once=true") as resp:
        body = "".join(
            chunk.decode("utf-8", errors="replace")
            for chunk in resp.iter_raw()
        )

    assert resp.status_code == 200
    assert "event: snapshot" in body
    assert "event: agent_state" in body
    assert "event: permission_required" in body
    assert "agent-xyz" in body
