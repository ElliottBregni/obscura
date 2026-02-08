"""E2E Tests: Workflows (Phase 4)."""

import pytest


@pytest.mark.e2e
class TestWorkflows:
    """Test workflow functionality."""

    def test_create_workflow(self, client):
        """Can create a workflow."""
        resp = client.post("/api/v1/workflows", json={
            "name": "code-review-pipeline",
            "description": "Multi-step code review",
            "steps": [
                {
                    "name": "security-review",
                    "agent_template": "security-reviewer",
                    "input": "Review this code for security: {{code}}"
                },
                {
                    "name": "performance-review",
                    "agent_template": "performance-reviewer",
                    "input": "Review this code for performance: {{code}}",
                    "depends_on": ["security-review"]
                }
            ]
        })
        
        assert resp.status_code == 200
        data = resp.json()
        assert "workflow_id" in data
        assert data["name"] == "code-review-pipeline"
        assert len(data["steps"]) == 2

    def test_list_workflows(self, client):
        """Can list workflows."""
        # Create a workflow
        client.post("/api/v1/workflows", json={
            "name": "test-workflow",
            "steps": []
        })
        
        resp = client.get("/api/v1/workflows")
        
        assert resp.status_code == 200
        data = resp.json()
        assert "workflows" in data
        assert data["count"] >= 1

    def test_get_workflow(self, client):
        """Can get a specific workflow."""
        # Create workflow
        create_resp = client.post("/api/v1/workflows", json={
            "name": "get-test-workflow",
            "steps": [{"name": "step1"}]
        })
        workflow_id = create_resp.json()["workflow_id"]
        
        resp = client.get(f"/api/v1/workflows/{workflow_id}")
        
        assert resp.status_code == 200
        data = resp.json()
        assert data["workflow_id"] == workflow_id
        assert data["name"] == "get-test-workflow"

    def test_get_workflow_not_found(self, client):
        """Getting non-existent workflow returns 404."""
        resp = client.get("/api/v1/workflows/non-existent")
        
        assert resp.status_code == 404

    def test_delete_workflow(self, client):
        """Can delete a workflow."""
        # Create workflow
        create_resp = client.post("/api/v1/workflows", json={
            "name": "delete-test-workflow"
        })
        workflow_id = create_resp.json()["workflow_id"]
        
        resp = client.delete(f"/api/v1/workflows/{workflow_id}")
        
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_execute_workflow(self, client):
        """Can execute a workflow."""
        # Create workflow
        workflow = client.post("/api/v1/workflows", json={
            "name": "execute-test",
            "steps": [
                {
                    "name": "step1",
                    "input": "Process this: {{input_data}}"
                }
            ]
        }).json()
        
        # Execute
        resp = client.post(f"/api/v1/workflows/{workflow['workflow_id']}/execute", json={
            "inputs": {"input_data": "test data"}
        })
        
        assert resp.status_code == 200
        data = resp.json()
        assert "execution_id" in data
        assert data["status"] in ["completed", "failed"]
        assert "step_results" in data

    def test_execute_workflow_not_found(self, client):
        """Executing non-existent workflow returns 404."""
        resp = client.post("/api/v1/workflows/non-existent/execute", json={
            "inputs": {}
        })
        
        assert resp.status_code == 404

    def test_list_workflow_executions(self, client):
        """Can list executions for a workflow."""
        # Create and execute workflow
        workflow = client.post("/api/v1/workflows", json={
            "name": "list-executions-test",
            "steps": [{"name": "step1"}]
        }).json()
        
        client.post(f"/api/v1/workflows/{workflow['workflow_id']}/execute", json={
            "inputs": {}
        })
        
        # List executions
        resp = client.get(f"/api/v1/workflows/{workflow['workflow_id']}/executions")
        
        assert resp.status_code == 200
        data = resp.json()
        assert data["workflow_id"] == workflow["workflow_id"]
        assert len(data["executions"]) >= 1

    def test_get_execution(self, client):
        """Can get a specific execution."""
        # Create and execute workflow
        workflow = client.post("/api/v1/workflows", json={
            "name": "get-execution-test",
            "steps": [{"name": "step1"}]
        }).json()
        
        execution = client.post(f"/api/v1/workflows/{workflow['workflow_id']}/execute", json={
            "inputs": {}
        }).json()
        
        # Get execution
        resp = client.get(f"/api/v1/workflows/executions/{execution['execution_id']}")
        
        assert resp.status_code == 200
        data = resp.json()
        assert data["execution_id"] == execution["execution_id"]
        assert data["workflow_id"] == workflow["workflow_id"]

    def test_get_execution_not_found(self, client):
        """Getting non-existent execution returns 404."""
        resp = client.get("/api/v1/workflows/executions/non-existent")
        
        assert resp.status_code == 404
