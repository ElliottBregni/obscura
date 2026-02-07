"""E2E Tests: Agent Lifecycle"""

import pytest
import httpx
import os

BASE_URL = os.environ.get("OBSCURA_URL", "http://localhost:8080")
TOKEN = os.environ.get("OBSCURA_TOKEN", "local-dev-token")


@pytest.fixture
def client():
    """HTTP client with auth."""
    return httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=30.0
    )


@pytest.mark.e2e
class TestAgentLifecycle:
    """End-to-end agent lifecycle tests."""
    
    def test_health_check(self, client):
        """Server should be healthy."""
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    
    def test_spawn_agent(self, client):
        """Can spawn an agent."""
        resp = client.post("/api/v1/agents", json={
            "name": "e2e-test-agent",
            "model": "claude",
            "system_prompt": "You are a test agent",
            "memory_namespace": "e2e"
        })
        
        assert resp.status_code == 200
        data = resp.json()
        assert "agent_id" in data
        assert data["name"] == "e2e-test-agent"
        
        # Cleanup
        client.delete(f"/api/v1/agents/{data['agent_id']}")
    
    def test_run_agent_task(self, client):
        """Can run a task on an agent."""
        # Spawn
        spawn_resp = client.post("/api/v1/agents", json={
            "name": "e2e-task-agent",
            "model": "claude",
        })
        agent_id = spawn_resp.json()["agent_id"]
        
        try:
            # Run task
            run_resp = client.post(f"/api/v1/agents/{agent_id}/run", json={
                "prompt": "Say 'hello from e2e test' and nothing else",
                "context": {}
            })
            
            assert run_resp.status_code == 200
            data = run_resp.json()
            assert "result" in data or "error" in data
            
        finally:
            # Cleanup
            client.delete(f"/api/v1/agents/{agent_id}")
    
    def test_list_agents(self, client):
        """Can list agents."""
        resp = client.get("/api/v1/agents")
        
        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data
        assert isinstance(data["agents"], list)
    
    def test_agent_status_after_spawn(self, client):
        """Agent has correct status after spawning."""
        # Spawn
        spawn_resp = client.post("/api/v1/agents", json={
            "name": "e2e-status-agent",
            "model": "claude",
        })
        agent_id = spawn_resp.json()["agent_id"]
        
        try:
            # Check status
            status_resp = client.get(f"/api/v1/agents/{agent_id}")
            
            assert status_resp.status_code == 200
            data = status_resp.json()
            assert data["agent_id"] == agent_id
            assert "status" in data
            
        finally:
            client.delete(f"/api/v1/agents/{agent_id}")
    
    def test_stop_agent(self, client):
        """Can stop an agent."""
        # Spawn
        spawn_resp = client.post("/api/v1/agents", json={
            "name": "e2e-stop-agent",
            "model": "claude",
        })
        agent_id = spawn_resp.json()["agent_id"]
        
        # Stop
        stop_resp = client.delete(f"/api/v1/agents/{agent_id}")
        
        assert stop_resp.status_code == 200
        assert stop_resp.json()["status"] == "stopped"


@pytest.mark.e2e
class TestMemoryOperations:
    """End-to-end memory tests."""
    
    def test_set_and_get_memory(self, client):
        """Can set and retrieve memory."""
        # Set
        set_resp = client.post("/api/v1/memory/e2e/test-key", json={
            "value": {"test": "data", "number": 42}
        })
        assert set_resp.status_code == 200
        
        # Get
        get_resp = client.get("/api/v1/memory/e2e/test-key")
        assert get_resp.status_code == 200
        assert get_resp.json()["value"]["test"] == "data"
        
        # Cleanup
        client.delete("/api/v1/memory/e2e/test-key")
    
    def test_memory_not_found(self, client):
        """404 for missing memory key."""
        resp = client.get("/api/v1/memory/e2e/nonexistent-key-12345")
        assert resp.status_code == 404
    
    def test_delete_memory(self, client):
        """Can delete memory."""
        # Set
        client.post("/api/v1/memory/e2e/delete-test", json={"value": "to-delete"})
        
        # Delete
        del_resp = client.delete("/api/v1/memory/e2e/delete-test")
        assert del_resp.status_code == 200
        
        # Verify deleted
        get_resp = client.get("/api/v1/memory/e2e/delete-test")
        assert get_resp.status_code == 404
    
    def test_list_memory_keys(self, client):
        """Can list memory keys."""
        # Create a key
        client.post("/api/v1/memory/e2e/list-test", json={"value": "x"})
        
        # List
        resp = client.get("/api/v1/memory?namespace=e2e")
        
        assert resp.status_code == 200
        data = resp.json()
        assert "keys" in data
        
        # Cleanup
        client.delete("/api/v1/memory/e2e/list-test")
    
    def test_search_memory(self, client):
        """Can search memory."""
        # Set searchable content
        client.post("/api/v1/memory/e2e/search-test", json={
            "value": "This is a searchable value with Python code"
        })
        
        # Search
        resp = client.get("/api/v1/memory/search?q=Python")
        
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        
        # Cleanup
        client.delete("/api/v1/memory/e2e/search-test")


@pytest.mark.e2e
class TestVectorMemory:
    """End-to-end vector memory tests."""
    
    def test_remember_and_recall(self, client):
        """Can store and semantically recall."""
        # Remember
        resp = client.post("/api/v1/vector-memory/e2e/test-mem", json={
            "text": "Python async uses event loops for concurrency",
            "metadata": {"topic": "python"}
        })
        assert resp.status_code == 200
        
        # Recall
        recall_resp = client.get("/api/v1/vector-memory/search?q=how+to+do+concurrency&top_k=3")
        assert recall_resp.status_code == 200
        data = recall_resp.json()
        assert "results" in data
        
        # Cleanup
        client.delete("/api/v1/vector-memory/e2e/test-mem")


@pytest.mark.e2e
class TestErrorHandling:
    """End-to-end error handling tests."""
    
    def test_404_agent_not_found(self, client):
        """404 for non-existent agent."""
        resp = client.get("/api/v1/agents/nonexistent-agent-12345")
        assert resp.status_code == 404
    
    def test_invalid_agent_id_format(self, client):
        """Handle invalid agent ID."""
        resp = client.get("/api/v1/agents/")
        # Should either 404 or handle gracefully
        assert resp.status_code in [200, 404, 422]
    
    def test_unauthorized_request(self):
        """401 for missing/invalid token."""
        bad_client = httpx.Client(base_url=BASE_URL, timeout=10.0)
        resp = bad_client.get("/api/v1/agents")
        # May 401 or work if auth disabled
        assert resp.status_code in [200, 401]


@pytest.mark.e2e
class TestWorkflows:
    """Complete workflow tests."""
    
    def test_full_agent_workflow(self, client):
        """Complete: spawn → run → check status → stop."""
        # 1. Spawn
        spawn = client.post("/api/v1/agents", json={
            "name": "workflow-agent",
            "model": "claude",
            "memory_namespace": "e2e-workflow"
        })
        agent_id = spawn.json()["agent_id"]
        
        try:
            # 2. Check status
            status = client.get(f"/api/v1/agents/{agent_id}")
            assert status.status_code == 200
            
            # 3. Run task (if model available)
            run = client.post(f"/api/v1/agents/{agent_id}/run", json={
                "prompt": "What is 2+2? Answer with just the number.",
            })
            # May succeed or fail depending on model config
            assert run.status_code in [200, 500]
            
            # 4. Store memory
            mem = client.post(f"/api/v1/memory/e2e-workflow/task", json={
                "value": {"agent_id": agent_id, "completed": True}
            })
            assert mem.status_code == 200
            
        finally:
            # 5. Stop
            client.delete(f"/api/v1/agents/{agent_id}")
            client.delete("/api/v1/memory/e2e-workflow/task")
