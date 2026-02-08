"""E2E Tests: Agent Bulk Operations, Templates, and Tags."""

import pytest


@pytest.mark.e2e
class TestAgentBulkOperations:
    """Test bulk agent operations."""

    def test_bulk_spawn_agents(self, client):
        """Can spawn multiple agents at once."""
        resp = client.post("/api/v1/agents/bulk", json={
            "agents": [
                {"name": "bulk-agent-1", "model": "claude"},
                {"name": "bulk-agent-2", "model": "claude"},
                {"name": "bulk-agent-3", "model": "copilot"},
            ]
        })
        
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_created"] == 3
        assert len(data["created"]) == 3
        assert len(data["errors"]) == 0
        
        # Cleanup
        agent_ids = [a["agent_id"] for a in data["created"]]
        for aid in agent_ids:
            client.delete(f"/api/v1/agents/{aid}")

    def test_bulk_spawn_empty_list(self, client):
        """Bulk spawn with empty list returns error."""
        resp = client.post("/api/v1/agents/bulk", json={"agents": []})
        
        assert resp.status_code == 400
        assert "No agents provided" in resp.json()["detail"]

    def test_bulk_spawn_too_many(self, client):
        """Bulk spawn limited to 100 agents."""
        resp = client.post("/api/v1/agents/bulk", json={
            "agents": [{"name": f"agent-{i}"} for i in range(101)]
        })
        
        assert resp.status_code == 400
        assert "Cannot spawn more than 100" in resp.json()["detail"]

    def test_bulk_stop_agents(self, client):
        """Can stop multiple agents at once."""
        # Create some agents
        spawn_resp = client.post("/api/v1/agents/bulk", json={
            "agents": [
                {"name": "stop-test-1"},
                {"name": "stop-test-2"},
            ]
        })
        agent_ids = [a["agent_id"] for a in spawn_resp.json()["created"]]
        
        # Stop them using POST /api/v1/agents/bulk/stop instead
        stop_resp = client.post("/api/v1/agents/bulk/stop", json={
            "agent_ids": agent_ids
        })
        
        assert stop_resp.status_code == 200
        data = stop_resp.json()
        assert data["total_stopped"] == 2
        assert len(data["stopped"]) == 2

    def test_bulk_stop_empty_list(self, client):
        """Bulk stop with empty list returns error."""
        resp = client.delete("/api/v1/agents/bulk", json={"agent_ids": []})
        
        assert resp.status_code == 400
        assert "No agent_ids provided" in resp.json()["detail"]

    def test_bulk_tag_agents(self, client):
        """Can tag multiple agents at once."""
        # Create agents
        spawn_resp = client.post("/api/v1/agents/bulk", json={
            "agents": [{"name": "tag-test-1"}, {"name": "tag-test-2"}]
        })
        agent_ids = [a["agent_id"] for a in spawn_resp.json()["created"]]
        
        # Tag them
        tag_resp = client.post("/api/v1/agents/bulk/tag", json={
            "agent_ids": agent_ids,
            "tags": ["production", "critical"]
        })
        
        assert tag_resp.status_code == 200
        data = tag_resp.json()
        assert len(data["tagged"]) == 2
        
        # Cleanup
        client.delete("/api/v1/agents/bulk", json={"agent_ids": agent_ids})


@pytest.mark.e2e
class TestAgentTemplates:
    """Test agent template functionality."""

    def test_create_template(self, client):
        """Can create an agent template."""
        resp = client.post("/api/v1/agent-templates", json={
            "name": "code-reviewer",
            "model": "claude",
            "system_prompt": "You are a code reviewer focused on security.",
            "timeout_seconds": 300,
            "max_iterations": 5,
            "tags": ["security", "review"]
        })
        
        assert resp.status_code == 200
        data = resp.json()
        assert "template_id" in data
        assert data["name"] == "code-reviewer"
        assert data["model"] == "claude"
        assert data["system_prompt"] == "You are a code reviewer focused on security."
        
        return data["template_id"]

    def test_list_templates(self, client):
        """Can list all templates."""
        # Create a template first
        client.post("/api/v1/agent-templates", json={
            "name": "test-template",
            "model": "claude"
        })
        
        resp = client.get("/api/v1/agent-templates")
        
        assert resp.status_code == 200
        data = resp.json()
        assert "templates" in data
        assert data["count"] >= 1

    def test_get_template(self, client):
        """Can get a specific template."""
        # Create template
        create_resp = client.post("/api/v1/agent-templates", json={
            "name": "get-test-template",
            "model": "copilot"
        })
        template_id = create_resp.json()["template_id"]
        
        # Get it
        resp = client.get(f"/api/v1/agent-templates/{template_id}")
        
        assert resp.status_code == 200
        data = resp.json()
        assert data["template_id"] == template_id
        assert data["name"] == "get-test-template"

    def test_get_template_not_found(self, client):
        """Getting non-existent template returns 404."""
        resp = client.get("/api/v1/agent-templates/non-existent-id")
        
        assert resp.status_code == 404

    def test_delete_template(self, client):
        """Can delete a template."""
        # Create template
        create_resp = client.post("/api/v1/agent-templates", json={
            "name": "delete-test-template"
        })
        template_id = create_resp.json()["template_id"]
        
        # Delete it
        resp = client.delete(f"/api/v1/agent-templates/{template_id}")
        
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        
        # Verify it's gone
        get_resp = client.get(f"/api/v1/agent-templates/{template_id}")
        assert get_resp.status_code == 404

    def test_spawn_from_template(self, client):
        """Can spawn agent from template."""
        # Create template
        create_resp = client.post("/api/v1/agent-templates", json={
            "name": "spawn-test-template",
            "model": "claude",
            "system_prompt": "You are a test agent.",
            "tags": ["test"]
        })
        template_id = create_resp.json()["template_id"]
        
        # Spawn from template
        resp = client.post("/api/v1/agents/from-template", json={
            "template_id": template_id,
            "name": "my-template-instance"
        })
        
        assert resp.status_code == 200
        data = resp.json()
        assert "agent_id" in data
        assert data["name"] == "my-template-instance"
        assert data["template_id"] == template_id
        
        # Cleanup
        client.delete(f"/api/v1/agents/{data['agent_id']}")

    def test_spawn_from_template_not_found(self, client):
        """Spawning from non-existent template returns 404."""
        resp = client.post("/api/v1/agents/from-template", json={
            "template_id": "non-existent",
            "name": "test"
        })
        
        assert resp.status_code == 404


@pytest.mark.e2e
class TestAgentTags:
    """Test agent tag functionality."""

    def test_add_tags_to_agent(self, client):
        """Can add tags to an agent."""
        # Create agent
        spawn_resp = client.post("/api/v1/agents", json={
            "name": "tag-test-agent"
        })
        agent_id = spawn_resp.json()["agent_id"]
        
        try:
            # Add tags
            resp = client.post(f"/api/v1/agents/{agent_id}/tags", json={
                "tags": ["production", "critical", "team-alpha"]
            })
            
            assert resp.status_code == 200
            data = resp.json()
            assert set(data["tags"]) == {"production", "critical", "team-alpha"}
            assert data["added"] == ["production", "critical", "team-alpha"]
        finally:
            client.delete(f"/api/v1/agents/{agent_id}")

    def test_remove_tags_from_agent(self, client):
        """Can remove tags from an agent."""
        # Create agent and add tags
        spawn_resp = client.post("/api/v1/agents", json={
            "name": "tag-remove-test"
        })
        agent_id = spawn_resp.json()["agent_id"]
        
        try:
            client.post(f"/api/v1/agents/{agent_id}/tags", json={
                "tags": ["tag1", "tag2", "tag3"]
            })
            
            # Remove some tags
            resp = client.delete(f"/api/v1/agents/{agent_id}/tags", json={
                "tags": ["tag1", "tag2"]
            })
            
            assert resp.status_code == 200
            data = resp.json()
            assert data["tags"] == ["tag3"]
            assert set(data["removed"]) == {"tag1", "tag2"}
        finally:
            client.delete(f"/api/v1/agents/{agent_id}")

    def test_get_agent_tags(self, client):
        """Can get tags for an agent."""
        # Create agent with tags
        spawn_resp = client.post("/api/v1/agents", json={
            "name": "get-tags-test"
        })
        agent_id = spawn_resp.json()["agent_id"]
        
        try:
            client.post(f"/api/v1/agents/{agent_id}/tags", json={
                "tags": ["test-tag-1", "test-tag-2"]
            })
            
            resp = client.get(f"/api/v1/agents/{agent_id}/tags")
            
            assert resp.status_code == 200
            data = resp.json()
            assert set(data["tags"]) == {"test-tag-1", "test-tag-2"}
        finally:
            client.delete(f"/api/v1/agents/{agent_id}")

    def test_filter_agents_by_tags(self, client):
        """Can filter agents by tags."""
        # Create agents with different tags
        spawn_resp = client.post("/api/v1/agents/bulk", json={
            "agents": [
                {"name": "filter-test-1", "tags": ["production"]},
                {"name": "filter-test-2", "tags": ["production", "critical"]},
                {"name": "filter-test-3", "tags": ["staging"]},
            ]
        })
        agent_ids = [a["agent_id"] for a in spawn_resp.json()["created"]]
        
        # Add tags via API
        for aid in agent_ids[:2]:  # First two get production tag
            client.post(f"/api/v1/agents/{aid}/tags", json={"tags": ["production"]})
        client.post(f"/api/v1/agents/{agent_ids[1]}/tags", json={"tags": ["critical"]})  # Second gets critical
        client.post(f"/api/v1/agents/{agent_ids[2]}/tags", json={"tags": ["staging"]})
        
        # Filter by production tag
        resp = client.get("/api/v1/agents?tags=production")
        
        assert resp.status_code == 200
        data = resp.json()
        # Should find at least 2 agents (we created 2 with production tag)
        production_agents = [a for a in data["agents"] if "production" in a.get("tags", [])]
        assert len(production_agents) >= 2
        
        # Cleanup
        client.delete("/api/v1/agents/bulk", json={"agent_ids": agent_ids})

    def test_filter_agents_by_name(self, client):
        """Can filter agents by name."""
        # Create agents
        spawn_resp = client.post("/api/v1/agents/bulk", json={
            "agents": [
                {"name": "web-server-1"},
                {"name": "web-server-2"},
                {"name": "db-server-1"},
            ]
        })
        agent_ids = [a["agent_id"] for a in spawn_resp.json()["created"]]
        
        # Filter by name
        resp = client.get("/api/v1/agents?name=web")
        
        assert resp.status_code == 200
        data = resp.json()
        web_agents = [a for a in data["agents"] if "web" in a["name"]]
        assert len(web_agents) >= 2
        
        # Cleanup
        client.delete("/api/v1/agents/bulk", json={"agent_ids": agent_ids})
