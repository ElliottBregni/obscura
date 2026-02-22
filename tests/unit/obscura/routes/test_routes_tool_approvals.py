"""Tests for tool approval routes."""

from __future__ import annotations

from starlette.testclient import TestClient

from obscura.approvals import clear_tool_approvals, create_tool_approval_request
from obscura.core.config import ObscuraConfig


def _make_client() -> TestClient:
    from obscura.server import create_app

    app = create_app(ObscuraConfig(auth_enabled=False, otel_enabled=False))
    return TestClient(app)


def test_tool_approvals_list_and_resolve() -> None:
    import asyncio

    asyncio.run(clear_tool_approvals())
    created = asyncio.run(
        create_tool_approval_request(
            user_id="anonymous",
            agent_id="agent-1",
            tool_use_id="tool-u-1",
            tool_name="run_shell",
            tool_input={"script": "ls"},
        )
    )

    client = _make_client()

    list_resp = client.get("/api/v1/tool-approvals")
    assert list_resp.status_code == 200
    data = list_resp.json()
    assert data["count"] == 1
    assert data["approvals"][0]["approval_id"] == created.approval_id

    resolve_resp = client.post(
        f"/api/v1/tool-approvals/{created.approval_id}/resolve",
        json={"approved": True, "reason": "ok"},
    )
    assert resolve_resp.status_code == 200
    resolved = resolve_resp.json()
    assert resolved["status"] == "approved"
    assert resolved["decision_reason"] == "ok"

    pending_resp = client.get("/api/v1/tool-approvals", params={"status": "pending"})
    assert pending_resp.status_code == 200
    assert pending_resp.json()["count"] == 0


def test_tool_approvals_resolve_validation() -> None:
    client = _make_client()
    resp = client.post(
        "/api/v1/tool-approvals/approval-missing/resolve",
        json={"approved": "yes"},
    )
    assert resp.status_code == 400
    assert "approved must be true or false" in resp.json()["detail"]
