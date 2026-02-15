"""Tests for sdk.routes.workflows — Workflow CRUD and execution."""

from __future__ import annotations

from typing import Any

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from starlette.testclient import TestClient
from sdk.config import ObscuraConfig


@pytest.fixture
def app() -> Any:
    config = ObscuraConfig(auth_enabled=False, otel_enabled=False)
    from sdk.server import create_app

    return create_app(config)


@pytest.fixture
def client(app: Any) -> TestClient:
    return TestClient(app)


class TestWorkflowExecution:
    @patch("sdk.routes.workflows.get_runtime")
    def test_execute_workflow(self, mock_get_runtime: Any, client: TestClient) -> None:
        mock_agent: Any = MagicMock()
        mock_agent.start = AsyncMock()
        mock_agent.run = AsyncMock(return_value="step result")
        mock_agent.stop = AsyncMock()
        mock_agent.status.name = "running"
        mock_agent.config.name = "wf-step"

        mock_runtime: Any = AsyncMock()
        mock_runtime.spawn = MagicMock(return_value=mock_agent)
        mock_get_runtime.return_value = mock_runtime

        # Create workflow
        create = client.post(
            "/api/v1/workflows",
            json={
                "name": "test-wf",
                "steps": [
                    {"name": "step1", "input": "do something"},
                ],
            },
        )
        wid = create.json()["workflow_id"]

        # Execute
        resp = client.post(
            f"/api/v1/workflows/{wid}/execute",
            json={
                "inputs": {},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert "step1" in data["step_results"]

    @patch("sdk.routes.workflows.get_runtime")
    def test_execute_workflow_not_found(self, mock_get_runtime: Any, client: TestClient) -> None:
        mock_runtime: Any = AsyncMock()
        mock_get_runtime.return_value = mock_runtime
        resp = client.post(
            "/api/v1/workflows/nonexistent/execute",
            json={
                "inputs": {},
            },
        )
        assert resp.status_code == 404

    @patch("sdk.routes.workflows.get_runtime")
    def test_execute_workflow_with_inputs(self, mock_get_runtime: Any, client: TestClient) -> None:
        mock_agent: Any = MagicMock()
        mock_agent.start = AsyncMock()
        mock_agent.run = AsyncMock(return_value="processed data")
        mock_agent.stop = AsyncMock()

        mock_runtime: Any = AsyncMock()
        mock_runtime.spawn = MagicMock(return_value=mock_agent)
        mock_get_runtime.return_value = mock_runtime

        create = client.post(
            "/api/v1/workflows",
            json={
                "name": "input-wf",
                "steps": [
                    {"name": "s1", "input": "Process {{data}}"},
                ],
            },
        )
        wid = create.json()["workflow_id"]

        resp = client.post(
            f"/api/v1/workflows/{wid}/execute",
            json={
                "inputs": {"data": "hello world"},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    @patch("sdk.routes.workflows.get_runtime")
    def test_execute_workflow_step_fails(self, mock_get_runtime: Any, client: TestClient) -> None:
        mock_agent: Any = MagicMock()
        mock_agent.start = AsyncMock()
        mock_agent.run = AsyncMock(side_effect=RuntimeError("step failed"))
        mock_agent.stop = AsyncMock()

        mock_runtime: Any = AsyncMock()
        mock_runtime.spawn = MagicMock(return_value=mock_agent)
        mock_get_runtime.return_value = mock_runtime

        create = client.post(
            "/api/v1/workflows",
            json={
                "name": "fail-wf",
                "steps": [{"name": "s1", "input": "do"}],
            },
        )
        wid = create.json()["workflow_id"]

        resp = client.post(f"/api/v1/workflows/{wid}/execute", json={"inputs": {}})
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"

    @patch("sdk.routes.workflows.get_runtime")
    def test_execute_multi_step_workflow(self, mock_get_runtime: Any, client: TestClient) -> None:
        call_count = 0

        async def run_step(prompt: Any, **ctx: Any) -> str:
            nonlocal call_count
            call_count += 1
            return f"result-{call_count}"

        mock_agent: Any = MagicMock()
        mock_agent.start = AsyncMock()
        mock_agent.run = AsyncMock(side_effect=run_step)
        mock_agent.stop = AsyncMock()

        mock_runtime: Any = AsyncMock()
        mock_runtime.spawn = MagicMock(return_value=mock_agent)
        mock_get_runtime.return_value = mock_runtime

        create = client.post(
            "/api/v1/workflows",
            json={
                "name": "multi-wf",
                "steps": [
                    {"name": "s1", "input": "first"},
                    {"name": "s2", "input": "second with {{s1.output}}"},
                ],
            },
        )
        wid = create.json()["workflow_id"]

        resp = client.post(f"/api/v1/workflows/{wid}/execute", json={"inputs": {}})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert "s1" in data["step_results"]
        assert "s2" in data["step_results"]


class TestWorkflowExecutions:
    @patch("sdk.routes.workflows.get_runtime")
    def test_list_executions(self, mock_get_runtime: Any, client: TestClient) -> None:
        mock_agent: Any = MagicMock()
        mock_agent.start = AsyncMock()
        mock_agent.run = AsyncMock(return_value="ok")
        mock_agent.stop = AsyncMock()

        mock_runtime: Any = AsyncMock()
        mock_runtime.spawn = MagicMock(return_value=mock_agent)
        mock_get_runtime.return_value = mock_runtime

        create = client.post(
            "/api/v1/workflows",
            json={
                "name": "exec-wf",
                "steps": [{"name": "s1", "input": "x"}],
            },
        )
        wid = create.json()["workflow_id"]

        # Execute it
        exec_resp = client.post(f"/api/v1/workflows/{wid}/execute", json={"inputs": {}})
        exec_id = exec_resp.json()["execution_id"]

        # List executions
        resp = client.get(f"/api/v1/workflows/{wid}/executions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1

        # Get specific execution
        resp2 = client.get(f"/api/v1/workflows/executions/{exec_id}")
        assert resp2.status_code == 200

    def test_list_executions_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/workflows/nonexistent/executions")
        assert resp.status_code == 404

    def test_get_execution_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/workflows/executions/nonexistent")
        assert resp.status_code == 404
