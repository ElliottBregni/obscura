"""E2E Tests: Agent Bulk Operations, Templates, and Tags."""

from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response


@pytest.mark.e2e
class TestAgentBulkOperations:
    """Test bulk agent operations."""

    def test_bulk_spawn_agents(self, client: TestClient) -> None:
        """Can spawn multiple agents at once."""
        resp: Response = client.post("/api/v1/agents/bulk", json={
            "agents": [
                {"name": "bulk-agent-1", "model": "claude"},
                {"name": "bulk-agent-2", "model": "claude"},
                {"name": "bulk-agent-3", "model": "copilot"},
            ]
        })

        assert resp.status_code == 200
        data: dict[str, Any] = cast(dict[str, Any], resp.json())
        assert data["total_created"] == 3
        created: list[dict[str, Any]] = cast(list[dict[str, Any]], data["created"])
        errors: list[Any] = cast(list[Any], data["errors"])
        assert len(created) == 3
        assert len(errors) == 0

        # Cleanup
        agent_ids = [a["agent_id"] for a in created]
        for aid in agent_ids:
            client.delete(f"/api/v1/agents/{aid}")

    def test_bulk_spawn_empty_list(self, client: TestClient) -> None:
        """Bulk spawn with empty list returns error."""
        resp: Response = client.post("/api/v1/agents/bulk", json={"agents": []})

        assert resp.status_code == 400
        data: dict[str, Any] = cast(dict[str, Any], resp.json())
        assert "No agents provided" in data["detail"]

    def test_bulk_spawn_too_many(self, client: TestClient) -> None:
        """Bulk spawn limited to 100 agents."""
        resp: Response = client.post("/api/v1/agents/bulk", json={
            "agents": [{"name": f"agent-{i}"} for i in range(101)]
        })

        assert resp.status_code == 400
        data: dict[str, Any] = cast(dict[str, Any], resp.json())
        assert "Cannot spawn more than 100" in data["detail"]

    def test_bulk_stop_agents(self, client: TestClient) -> None:
        """Can stop multiple agents at once."""
        # Create some agents
        spawn_resp: Response = client.post("/api/v1/agents/bulk", json={
            "agents": [
                {"name": "stop-test-1"},
                {"name": "stop-test-2"},
            ]
        })
        spawn_data: dict[str, Any] = cast(dict[str, Any], spawn_resp.json())
        created: list[dict[str, Any]] = cast(list[dict[str, Any]], spawn_data["created"])
        agent_ids = [a["agent_id"] for a in created]

        # Stop them using POST /api/v1/agents/bulk/stop instead
        stop_resp: Response = client.post("/api/v1/agents/bulk/stop", json={
            "agent_ids": agent_ids
        })

        assert stop_resp.status_code == 200
        data: dict[str, Any] = cast(dict[str, Any], stop_resp.json())
        assert data["total_stopped"] == 2
        stopped: list[Any] = cast(list[Any], data["stopped"])
        assert len(stopped) == 2

    def test_bulk_stop_empty_list(self, client: TestClient) -> None:
        """Bulk stop with empty list returns error."""
        resp: Response = client.post("/api/v1/agents/bulk/stop", json={"agent_ids": []})

        assert resp.status_code == 400
        data: dict[str, Any] = cast(dict[str, Any], resp.json())
        assert "No agent_ids provided" in data["detail"]

    def test_bulk_tag_agents(self, client: TestClient) -> None:
        """Can tag multiple agents at once."""
        # Create agents
        spawn_resp: Response = client.post("/api/v1/agents/bulk", json={
            "agents": [{"name": "tag-test-1"}, {"name": "tag-test-2"}]
        })
        spawn_data: dict[str, Any] = cast(dict[str, Any], spawn_resp.json())
        created_agents: list[dict[str, Any]] = cast(list[dict[str, Any]], spawn_data["created"])
        agent_ids = [a["agent_id"] for a in created_agents]

        # Tag them
        tag_resp: Response = client.post("/api/v1/agents/bulk/tag", json={
            "agent_ids": agent_ids,
            "tags": ["production", "critical"]
        })

        assert tag_resp.status_code == 200
        data: dict[str, Any] = cast(dict[str, Any], tag_resp.json())
        tagged: list[Any] = cast(list[Any], data["tagged"])
        assert len(tagged) == 2

        # Cleanup
        for aid in agent_ids:
            client.delete(f"/api/v1/agents/{aid}")


@pytest.mark.e2e
class TestAgentTemplates:
    """Test agent template functionality."""

    def test_create_template(self, client: TestClient) -> None:
        """Can create an agent template."""
        resp: Response = client.post("/api/v1/agent-templates", json={
            "name": "code-reviewer",
            "model": "claude",
            "system_prompt": "You are a code reviewer focused on security.",
            "timeout_seconds": 300,
            "max_iterations": 5,
            "tags": ["security", "review"]
        })

        assert resp.status_code == 200
        data: dict[str, Any] = cast(dict[str, Any], resp.json())
        assert "template_id" in data
        assert data["name"] == "code-reviewer"
        assert data["model"] == "claude"
        assert data["system_prompt"] == "You are a code reviewer focused on security."

    def test_list_templates(self, client: TestClient) -> None:
        """Can list all templates."""
        # Create a template first
        client.post("/api/v1/agent-templates", json={
            "name": "test-template",
            "model": "claude"
        })

        resp: Response = client.get("/api/v1/agent-templates")

        assert resp.status_code == 200
        data: dict[str, Any] = cast(dict[str, Any], resp.json())
        assert "templates" in data
        assert data["count"] >= 1

    def test_get_template(self, client: TestClient) -> None:
        """Can get a specific template."""
        # Create template
        create_resp: Response = client.post("/api/v1/agent-templates", json={
            "name": "get-test-template",
            "model": "copilot"
        })
        create_data: dict[str, Any] = cast(dict[str, Any], create_resp.json())
        template_id = str(create_data["template_id"])

        # Get it
        resp: Response = client.get(f"/api/v1/agent-templates/{template_id}")

        assert resp.status_code == 200
        data: dict[str, Any] = cast(dict[str, Any], resp.json())
        assert data["template_id"] == template_id
        assert data["name"] == "get-test-template"

    def test_get_template_not_found(self, client: TestClient) -> None:
        """Getting non-existent template returns 404."""
        resp: Response = client.get("/api/v1/agent-templates/non-existent-id")

        assert resp.status_code == 404

    def test_delete_template(self, client: TestClient) -> None:
        """Can delete a template."""
        # Create template
        create_resp: Response = client.post("/api/v1/agent-templates", json={
            "name": "delete-test-template"
        })
        create_data: dict[str, Any] = cast(dict[str, Any], create_resp.json())
        template_id = str(create_data["template_id"])

        # Delete it
        resp: Response = client.delete(f"/api/v1/agent-templates/{template_id}")

        assert resp.status_code == 200
        data: dict[str, Any] = cast(dict[str, Any], resp.json())
        assert data["deleted"] is True

        # Verify it's gone
        get_resp: Response = client.get(f"/api/v1/agent-templates/{template_id}")
        assert get_resp.status_code == 404

    def test_spawn_from_template(self, client: TestClient) -> None:
        """Can spawn agent from template."""
        # Create template
        create_resp: Response = client.post("/api/v1/agent-templates", json={
            "name": "spawn-test-template",
            "model": "claude",
            "system_prompt": "You are a test agent.",
            "tags": ["test"]
        })
        create_data: dict[str, Any] = cast(dict[str, Any], create_resp.json())
        template_id = str(create_data["template_id"])

        # Spawn from template
        resp: Response = client.post("/api/v1/agents/from-template", json={
            "template_id": template_id,
            "name": "my-template-instance"
        })

        assert resp.status_code == 200
        data: dict[str, Any] = cast(dict[str, Any], resp.json())
        assert "agent_id" in data
        assert data["name"] == "my-template-instance"
        assert data["template_id"] == template_id

        # Cleanup
        client.delete(f"/api/v1/agents/{data['agent_id']}")

    def test_spawn_from_template_not_found(self, client: TestClient) -> None:
        """Spawning from non-existent template returns 404."""
        resp: Response = client.post("/api/v1/agents/from-template", json={
            "template_id": "non-existent",
            "name": "test"
        })

        assert resp.status_code == 404


@pytest.mark.e2e
class TestAgentTags:
    """Test agent tag functionality."""

    def test_add_tags_to_agent(self, client: TestClient) -> None:
        """Can add tags to an agent."""
        # Create agent
        spawn_resp: Response = client.post("/api/v1/agents", json={
            "name": "tag-test-agent"
        })
        spawn_data: dict[str, Any] = cast(dict[str, Any], spawn_resp.json())
        agent_id = str(spawn_data["agent_id"])

        try:
            # Add tags
            resp: Response = client.post(f"/api/v1/agents/{agent_id}/tags", json={
                "tags": ["production", "critical", "team-alpha"]
            })

            assert resp.status_code == 200
            data: dict[str, Any] = cast(dict[str, Any], resp.json())
            assert set(data["tags"]) == {"production", "critical", "team-alpha"}
            assert set(data["added"]) == {"production", "critical", "team-alpha"}
        finally:
            client.delete(f"/api/v1/agents/{agent_id}")

    def test_remove_tags_from_agent(self, client: TestClient) -> None:
        """Can remove tags from an agent."""
        # Create agent and add tags
        spawn_resp: Response = client.post("/api/v1/agents", json={
            "name": "tag-remove-test"
        })
        agent_id = str(cast(dict[str, Any], spawn_resp.json())["agent_id"])

        try:
            client.post(f"/api/v1/agents/{agent_id}/tags", json={
                "tags": ["tag1", "tag2", "tag3"]
            })

            # Remove some tags
            resp: Response = client.post(f"/api/v1/agents/{agent_id}/tags/remove", json={
                "tags": ["tag1", "tag2"]
            })

            assert resp.status_code == 200
            data: dict[str, Any] = cast(dict[str, Any], resp.json())
            tags: list[str] = cast(list[str], data["tags"])
            removed: list[str] = cast(list[str], data["removed"])
            assert tags == ["tag3"]
            assert set(removed) == {"tag1", "tag2"}
        finally:
            client.delete(f"/api/v1/agents/{agent_id}")

    def test_get_agent_tags(self, client: TestClient) -> None:
        """Can get tags for an agent."""
        # Create agent with tags
        spawn_resp: Response = client.post("/api/v1/agents", json={
            "name": "get-tags-test"
        })
        agent_id = str(cast(dict[str, Any], spawn_resp.json())["agent_id"])

        try:
            client.post(f"/api/v1/agents/{agent_id}/tags", json={
                "tags": ["test-tag-1", "test-tag-2"]
            })

            resp: Response = client.get(f"/api/v1/agents/{agent_id}/tags")

            assert resp.status_code == 200
            data: dict[str, Any] = cast(dict[str, Any], resp.json())
            tags: list[str] = cast(list[str], data["tags"])
            assert set(tags) == {"test-tag-1", "test-tag-2"}
        finally:
            client.delete(f"/api/v1/agents/{agent_id}")

    def test_filter_agents_by_tags(self, client: TestClient) -> None:
        """Can filter agents by tags."""
        # Create agents with different tags
        spawn_resp: Response = client.post("/api/v1/agents/bulk", json={
            "agents": [
                {"name": "filter-test-1", "tags": ["production"]},
                {"name": "filter-test-2", "tags": ["production", "critical"]},
                {"name": "filter-test-3", "tags": ["staging"]},
            ]
        })
        spawn_data: dict[str, Any] = cast(dict[str, Any], spawn_resp.json())
        created_agents: list[dict[str, Any]] = cast(list[dict[str, Any]], spawn_data["created"])
        agent_ids = [a["agent_id"] for a in created_agents]

        # Add tags via API
        for aid in agent_ids[:2]:  # First two get production tag
            client.post(f"/api/v1/agents/{aid}/tags", json={"tags": ["production"]})
        client.post(f"/api/v1/agents/{agent_ids[1]}/tags", json={"tags": ["critical"]})  # Second gets critical
        client.post(f"/api/v1/agents/{agent_ids[2]}/tags", json={"tags": ["staging"]})

        # Filter by production tag
        resp: Response = client.get("/api/v1/agents?tags=production")

        assert resp.status_code == 200
        data: dict[str, Any] = cast(dict[str, Any], resp.json())
        agents_list: list[dict[str, Any]] = cast(list[dict[str, Any]], data["agents"])
        # Should find at least 2 agents (we created 2 with production tag)
        production_agents = [a for a in agents_list if "production" in cast(list[str], a.get("tags", []))]
        assert len(production_agents) >= 2

        # Cleanup
        for aid in agent_ids:
            client.delete(f"/api/v1/agents/{aid}")

    def test_filter_agents_by_name(self, client: TestClient) -> None:
        """Can filter agents by name."""
        # Create agents
        spawn_resp: Response = client.post("/api/v1/agents/bulk", json={
            "agents": [
                {"name": "web-server-1"},
                {"name": "web-server-2"},
                {"name": "db-server-1"},
            ]
        })
        spawn_data: dict[str, Any] = cast(dict[str, Any], spawn_resp.json())
        agent_ids = [a["agent_id"] for a in cast(list[dict[str, Any]], spawn_data["created"])]

        # Filter by name
        resp: Response = client.get("/api/v1/agents?name=web")

        assert resp.status_code == 200
        data: dict[str, Any] = cast(dict[str, Any], resp.json())
        web_agents = [a for a in cast(list[dict[str, Any]], data["agents"]) if "web" in a["name"]]
        assert len(web_agents) >= 2

        # Cleanup
        for aid in agent_ids:
            client.delete(f"/api/v1/agents/{aid}")
