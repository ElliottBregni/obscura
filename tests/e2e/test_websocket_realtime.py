"""E2E Tests: WebSocket & Real-time Features (Phase 3)."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient


@pytest.mark.e2e
class TestAgentGroups:
    """Test agent group functionality."""

    def test_create_group(self, client: TestClient) -> None:
        """Can create an agent group."""
        resp = client.post(
            "/api/v1/agent-groups",
            json={"name": "review-team", "agents": ["agent-1", "agent-2"]},
        )

        assert resp.status_code == 200
        data: Any = resp.json()
        assert "group_id" in data
        assert data["name"] == "review-team"
        assert data["agents"] == ["agent-1", "agent-2"]

    def test_list_groups(self, client: TestClient) -> None:
        """Can list agent groups."""
        # Create a group
        client.post("/api/v1/agent-groups", json={"name": "test-group", "agents": []})

        resp = client.get("/api/v1/agent-groups")

        assert resp.status_code == 200
        data: Any = resp.json()
        assert "groups" in data
        assert data["count"] >= 1

    def test_get_group(self, client: TestClient) -> None:
        """Can get a specific group."""
        # Create group
        create_resp = client.post(
            "/api/v1/agent-groups",
            json={"name": "get-test-group", "agents": ["agent-1"]},
        )
        create_data: Any = create_resp.json()
        group_id: str = create_data["group_id"]

        resp = client.get(f"/api/v1/agent-groups/{group_id}")

        assert resp.status_code == 200
        data: Any = resp.json()
        assert data["group_id"] == group_id
        assert data["name"] == "get-test-group"

    def test_get_group_not_found(self, client: TestClient) -> None:
        """Getting non-existent group returns 404."""
        resp = client.get("/api/v1/agent-groups/non-existent")

        assert resp.status_code == 404

    def test_delete_group(self, client: TestClient) -> None:
        """Can delete a group."""
        # Create group
        create_resp = client.post(
            "/api/v1/agent-groups", json={"name": "delete-test-group"}
        )
        create_data: Any = create_resp.json()
        group_id: str = create_data["group_id"]

        resp = client.delete(f"/api/v1/agent-groups/{group_id}")

        assert resp.status_code == 200
        data: Any = resp.json()
        assert data["deleted"] is True

    def test_broadcast_to_group(self, client: TestClient) -> None:
        """Can broadcast message to group."""
        # Create agents first
        agent1: Any = client.post("/api/v1/agents", json={"name": "broadcast-1"}).json()
        agent2: Any = client.post("/api/v1/agents", json={"name": "broadcast-2"}).json()

        # Create group with agents
        group: Any = client.post(
            "/api/v1/agent-groups",
            json={
                "name": "broadcast-group",
                "agents": [agent1["agent_id"], agent2["agent_id"]],
            },
        ).json()

        # Broadcast message
        resp = client.post(
            f"/api/v1/agent-groups/{group['group_id']}/broadcast",
            json={"message": "Review this code", "context": {"file": "main.py"}},
        )

        assert resp.status_code == 200
        data: Any = resp.json()
        assert data["group_id"] == group["group_id"]
        assert len(data["queued"]) == 2

        # Cleanup
        client.delete(f"/api/v1/agents/{agent1['agent_id']}")
        client.delete(f"/api/v1/agents/{agent2['agent_id']}")


@pytest.mark.e2e
class TestAgentMessaging:
    """Test agent-to-agent messaging."""

    def test_send_message_between_agents(self, client: TestClient) -> None:
        """Can send message from one agent to another."""
        # Create agents
        agent1: Any = client.post("/api/v1/agents", json={"name": "sender"}).json()
        agent2: Any = client.post("/api/v1/agents", json={"name": "receiver"}).json()

        try:
            resp = client.post(
                f"/api/v1/agents/{agent1['agent_id']}/send/{agent2['agent_id']}",
                json={
                    "message": "Can you review this?",
                    "context": {"file": "main.py"},
                },
            )

            assert resp.status_code == 200
            data: Any = resp.json()
            assert data["from_agent"] == agent1["agent_id"]
            assert data["to_agent"] == agent2["agent_id"]
            assert data["sent"] is True
        finally:
            client.delete(f"/api/v1/agents/{agent1['agent_id']}")
            client.delete(f"/api/v1/agents/{agent2['agent_id']}")

    def test_send_message_source_not_found(self, client: TestClient) -> None:
        """Sending from non-existent agent returns 404."""
        resp = client.post(
            "/api/v1/agents/non-existent/send/agent-2", json={"message": "test"}
        )

        assert resp.status_code == 404

    def test_send_message_target_not_found(self, client: TestClient) -> None:
        """Sending to non-existent agent returns 404."""
        agent: Any = client.post("/api/v1/agents", json={"name": "lonely"}).json()

        try:
            resp = client.post(
                f"/api/v1/agents/{agent['agent_id']}/send/non-existent",
                json={"message": "test"},
            )

            assert resp.status_code == 404
        finally:
            client.delete(f"/api/v1/agents/{agent['agent_id']}")

    def test_get_agent_messages(self, client: TestClient) -> None:
        """Can get messages for an agent."""
        agent: Any = client.post("/api/v1/agents", json={"name": "message-test"}).json()

        try:
            resp = client.get(f"/api/v1/agents/{agent['agent_id']}/messages")

            assert resp.status_code == 200
            data: Any = resp.json()
            assert data["agent_id"] == agent["agent_id"]
            assert "messages" in data
        finally:
            client.delete(f"/api/v1/agents/{agent['agent_id']}")


@pytest.mark.e2e
class TestWebSocketEndpoints:
    """Test WebSocket endpoints."""

    def test_agent_websocket(self, client: TestClient) -> None:
        """WebSocket endpoint for agent communication."""
        # Spawn an agent first so it exists in the runtime
        resp = client.post("/api/v1/agents", json={"name": "ws-test-agent"})
        agent_data: Any = resp.json()
        agent_id: str = agent_data["agent_id"]

        with client.websocket_connect(f"/ws/agents/{agent_id}?token=test") as ws:
            # Request status
            ws.send_json({"type": "status"})
            data: Any = ws.receive_json()
            assert data["type"] == "status"
            assert "status" in data

            # Request stop
            ws.send_json({"type": "stop"})
            data = ws.receive_json()
            assert data["type"] == "status"
            assert data["status"] == "STOPPED"

    def test_broadcast_websocket(self, client: TestClient) -> None:
        """WebSocket endpoint for broadcast events."""
        with client.websocket_connect("/ws/broadcast?token=test") as ws:
            ws.send_text("ping")
            assert ws.receive_text() == "pong"

    def test_memory_watch_websocket(self, client: TestClient) -> None:
        """WebSocket endpoint for memory watching."""
        with client.websocket_connect("/ws/memory/test-ns?token=test") as ws:
            # Should receive init message with current keys
            data: Any = ws.receive_json()
            assert data["type"] == "init"
            assert data["namespace"] == "test-ns"
            assert "keys" in data

            # Ping/pong keepalive
            ws.send_text("ping")
            assert ws.receive_text() == "pong"

    def test_monitor_websocket(self, client: TestClient) -> None:
        """WebSocket endpoint for monitoring."""
        with client.websocket_connect("/ws/monitor?token=test") as ws:
            # Should receive init message with agents list
            data: Any = ws.receive_json()
            assert data["type"] == "init"
            assert "agents" in data
