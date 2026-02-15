"""Tests for sdk.routes.agent_groups — Agent groups CRUD and messaging."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from starlette.testclient import TestClient
from sdk.config import ObscuraConfig


@pytest.fixture
def app():
    config = ObscuraConfig(auth_enabled=False, otel_enabled=False)
    from sdk.server import create_app

    return create_app(config)


@pytest.fixture
def client(app):
    return TestClient(app)


class TestAgentGroupCRUD:
    def test_create_group(self, client):
        resp = client.post(
            "/api/v1/agent-groups",
            json={
                "name": "my-group",
                "agents": ["a1", "a2"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "my-group"
        assert "group_id" in data
        assert data["agents"] == ["a1", "a2"]

    def test_list_groups(self, client):
        client.post("/api/v1/agent-groups", json={"name": "g1"})
        resp = client.get("/api/v1/agent-groups")
        assert resp.status_code == 200
        data = resp.json()
        assert "groups" in data
        assert isinstance(data["groups"], list)

    def test_get_group(self, client):
        create = client.post("/api/v1/agent-groups", json={"name": "g2"})
        gid = create.json()["group_id"]
        resp = client.get(f"/api/v1/agent-groups/{gid}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "g2"

    def test_get_group_not_found(self, client):
        resp = client.get("/api/v1/agent-groups/nonexistent")
        assert resp.status_code == 404

    def test_delete_group(self, client):
        create = client.post("/api/v1/agent-groups", json={"name": "g3"})
        gid = create.json()["group_id"]
        resp = client.delete(f"/api/v1/agent-groups/{gid}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_delete_group_not_found(self, client):
        resp = client.delete("/api/v1/agent-groups/nonexistent")
        assert resp.status_code == 404


class TestAgentGroupBroadcast:
    @patch("sdk.routes.agent_groups.get_runtime")
    def test_broadcast_to_group(self, mock_get_runtime, client):
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock()
        mock_runtime = AsyncMock()
        mock_runtime.get_agent = MagicMock(return_value=mock_agent)
        mock_get_runtime.return_value = mock_runtime

        create = client.post(
            "/api/v1/agent-groups",
            json={
                "name": "broadcast-group",
                "agents": ["a1"],
            },
        )
        gid = create.json()["group_id"]

        resp = client.post(
            f"/api/v1/agent-groups/{gid}/broadcast",
            json={
                "message": "hello all",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["group_id"] == gid
        assert len(data["queued"]) == 1

    @patch("sdk.routes.agent_groups.get_runtime")
    def test_broadcast_group_not_found(self, mock_get_runtime, client):
        mock_runtime = AsyncMock()
        mock_get_runtime.return_value = mock_runtime
        resp = client.post(
            "/api/v1/agent-groups/nonexistent/broadcast",
            json={
                "message": "hello",
            },
        )
        assert resp.status_code == 404

    @patch("sdk.routes.agent_groups.get_runtime")
    def test_broadcast_agent_not_found(self, mock_get_runtime, client):
        mock_runtime = AsyncMock()
        mock_runtime.get_agent = MagicMock(return_value=None)
        mock_get_runtime.return_value = mock_runtime

        create = client.post(
            "/api/v1/agent-groups",
            json={
                "name": "bad-agents",
                "agents": ["missing-agent"],
            },
        )
        gid = create.json()["group_id"]

        resp = client.post(
            f"/api/v1/agent-groups/{gid}/broadcast",
            json={
                "message": "hello",
            },
        )
        assert resp.status_code == 200
        assert len(resp.json()["errors"]) == 1


class TestAgentMessaging:
    @patch("sdk.routes.agent_groups.get_runtime")
    def test_send_message(self, mock_get_runtime, client):
        mock_agent = MagicMock()
        mock_agent.send_message = AsyncMock()
        mock_runtime = AsyncMock()
        mock_runtime.get_agent = MagicMock(return_value=mock_agent)
        mock_get_runtime.return_value = mock_runtime

        resp = client.post(
            "/api/v1/agents/a1/send/a2",
            json={
                "message": "hello from a1",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["sent"] is True

    @patch("sdk.routes.agent_groups.get_runtime")
    def test_send_message_source_not_found(self, mock_get_runtime, client):
        mock_runtime = AsyncMock()
        mock_runtime.get_agent = MagicMock(return_value=None)
        mock_get_runtime.return_value = mock_runtime

        resp = client.post(
            "/api/v1/agents/missing/send/a2",
            json={
                "message": "hello",
            },
        )
        assert resp.status_code == 404

    @patch("sdk.routes.agent_groups.get_runtime")
    def test_get_messages(self, mock_get_runtime, client):
        mock_agent = MagicMock()
        mock_runtime = AsyncMock()
        mock_runtime.get_agent = MagicMock(return_value=mock_agent)
        mock_get_runtime.return_value = mock_runtime

        resp = client.get("/api/v1/agents/a1/messages")
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == "a1"

    @patch("sdk.routes.agent_groups.get_runtime")
    def test_get_messages_not_found(self, mock_get_runtime, client):
        mock_runtime = AsyncMock()
        mock_runtime.get_agent = MagicMock(return_value=None)
        mock_get_runtime.return_value = mock_runtime

        resp = client.get("/api/v1/agents/missing/messages")
        assert resp.status_code == 404
