"""Tests for extended agent template CRUD and spawn-from-template endpoints."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from obscura.core.config import ObscuraConfig
from obscura.routes import template_store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_templates() -> Generator[None, None, None]:
    template_store.clear()
    yield
    template_store.clear()


@pytest.fixture
def app() -> FastAPI:
    config = ObscuraConfig(auth_enabled=False, otel_enabled=False)
    from obscura.server import create_app

    return create_app(config)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _make_mock_agent(
    agent_id: str = "agent-1",
    name: str = "test-agent",
    status_name: str = "WAITING",
) -> MagicMock:
    mock: MagicMock = MagicMock()
    mock.id = agent_id
    mock.config = MagicMock()
    mock.config.name = name
    mock.config.tags = []
    mock.status = MagicMock()
    mock.status.name = status_name
    mock.created_at = MagicMock()
    mock.created_at.isoformat.return_value = "2026-01-01T00:00:00+00:00"
    mock.start = AsyncMock()
    mock.client = MagicMock()
    mock.client.run_loop_to_completion = AsyncMock(return_value="aper result")
    mock.client.on = MagicMock()
    mock.client.backend_impl = MagicMock()
    mock.client.backend_impl.register_hook = MagicMock()
    return mock


def _make_mock_runtime(agent: MagicMock | None = None) -> AsyncMock:
    runtime = AsyncMock()
    runtime.spawn = MagicMock(return_value=agent or _make_mock_agent())
    return runtime


# ---------------------------------------------------------------------------
# Template store unit tests
# ---------------------------------------------------------------------------


class TestTemplateStore:
    def test_put_get(self) -> None:
        template_store.put("t1", {"name": "alpha"})
        assert template_store.get("t1") == {"name": "alpha"}

    def test_get_missing(self) -> None:
        assert template_store.get("nope") is None

    def test_delete(self) -> None:
        template_store.put("t1", {"name": "alpha"})
        assert template_store.delete("t1") is True
        assert template_store.get("t1") is None

    def test_delete_missing(self) -> None:
        assert template_store.delete("nope") is False

    def test_get_all(self) -> None:
        template_store.put("a", {"x": 1})
        template_store.put("b", {"x": 2})
        assert len(template_store.get_all()) == 2

    def test_clear(self) -> None:
        template_store.put("a", {"x": 1})
        template_store.clear()
        assert template_store.get_all() == {}


# ---------------------------------------------------------------------------
# POST /agent-templates (create with extended fields)
# ---------------------------------------------------------------------------


class TestTemplateCreate:
    def test_basic_create(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/agent-templates",
            json={"name": "basic"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "basic"
        assert data["model"] == "claude"
        assert data["aper_profile"] is None
        assert data["skills"] == []
        assert data["mcp_servers"] == []
        assert data["persist"] is False

    def test_create_with_aper_profile(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/agent-templates",
            json={
                "name": "researcher",
                "aper_profile": {
                    "analyze_template": "custom analyze",
                    "max_turns": 5,
                },
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["aper_profile"]["analyze_template"] == "custom analyze"
        assert data["aper_profile"]["max_turns"] == 5

    def test_create_with_skills(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/agent-templates",
            json={
                "name": "skilled",
                "skills": [
                    {"name": "code-review", "content": "Review code for bugs."},
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["skills"]) == 1
        assert data["skills"][0]["name"] == "code-review"

    def test_create_with_mcp_servers(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/agent-templates",
            json={
                "name": "mcp-agent",
                "mcp_servers": [
                    {"name": "pw", "transport": "stdio", "command": "npx", "args": ["-y", "@playwright/mcp"]},
                    {"name": "remote", "transport": "sse", "url": "http://localhost:3000"},
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["mcp_servers"]) == 2
        assert data["mcp_servers"][0]["transport"] == "stdio"
        assert data["mcp_servers"][1]["transport"] == "sse"

    def test_create_with_a2a(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/agent-templates",
            json={
                "name": "a2a-agent",
                "a2a_remote_tools": {"urls": ["http://peer:8080"]},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["a2a_remote_tools"]["urls"] == ["http://peer:8080"]

    def test_create_with_persist(self, client: TestClient) -> None:
        with patch.object(template_store, "persist_template") as mock_persist:
            resp = client.post(
                "/api/v1/agent-templates",
                json={"name": "durable", "persist": True},
            )
            assert resp.status_code == 200
            mock_persist.assert_called_once()

    def test_create_validation_error(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/agent-templates",
            json={"name": "bad", "max_iterations": 0},
        )
        assert resp.status_code == 422  # Pydantic validation


# ---------------------------------------------------------------------------
# GET /agent-templates
# ---------------------------------------------------------------------------


class TestTemplateList:
    def test_empty(self, client: TestClient) -> None:
        resp = client.get("/api/v1/agent-templates")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_after_create(self, client: TestClient) -> None:
        client.post("/api/v1/agent-templates", json={"name": "t1"})
        client.post("/api/v1/agent-templates", json={"name": "t2"})
        resp = client.get("/api/v1/agent-templates")
        assert resp.json()["count"] == 2


# ---------------------------------------------------------------------------
# GET /agent-templates/{template_id}
# ---------------------------------------------------------------------------


class TestTemplateGet:
    def test_found(self, client: TestClient) -> None:
        create_resp = client.post("/api/v1/agent-templates", json={"name": "t1"})
        tid = create_resp.json()["template_id"]
        resp = client.get(f"/api/v1/agent-templates/{tid}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "t1"

    def test_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/agent-templates/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /agent-templates/{template_id}
# ---------------------------------------------------------------------------


class TestTemplateUpdate:
    def test_partial_update(self, client: TestClient) -> None:
        create_resp = client.post("/api/v1/agent-templates", json={"name": "orig"})
        tid = create_resp.json()["template_id"]
        resp = client.put(f"/api/v1/agent-templates/{tid}", json={"name": "updated"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "updated"
        assert resp.json()["model"] == "claude"  # unchanged

    def test_update_aper_profile(self, client: TestClient) -> None:
        create_resp = client.post("/api/v1/agent-templates", json={"name": "orig"})
        tid = create_resp.json()["template_id"]
        resp = client.put(
            f"/api/v1/agent-templates/{tid}",
            json={"aper_profile": {"max_turns": 3}},
        )
        assert resp.status_code == 200
        assert resp.json()["aper_profile"]["max_turns"] == 3

    def test_update_not_found(self, client: TestClient) -> None:
        resp = client.put("/api/v1/agent-templates/nope", json={"name": "x"})
        assert resp.status_code == 404

    def test_update_persisted(self, client: TestClient) -> None:
        with patch.object(template_store, "persist_template") as mock_persist:
            create_resp = client.post(
                "/api/v1/agent-templates", json={"name": "durable", "persist": True}
            )
            tid = create_resp.json()["template_id"]
            mock_persist.reset_mock()
            client.put(f"/api/v1/agent-templates/{tid}", json={"name": "updated"})
            mock_persist.assert_called_once()


# ---------------------------------------------------------------------------
# DELETE /agent-templates/{template_id}
# ---------------------------------------------------------------------------


class TestTemplateDelete:
    def test_delete(self, client: TestClient) -> None:
        create_resp = client.post("/api/v1/agent-templates", json={"name": "t1"})
        tid = create_resp.json()["template_id"]
        resp = client.delete(f"/api/v1/agent-templates/{tid}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_delete_not_found(self, client: TestClient) -> None:
        resp = client.delete("/api/v1/agent-templates/nope")
        assert resp.status_code == 404

    def test_delete_persisted(self, client: TestClient) -> None:
        with patch.object(template_store, "persist_template"), patch.object(
            template_store, "delete_persisted"
        ) as mock_del:
            create_resp = client.post(
                "/api/v1/agent-templates", json={"name": "durable", "persist": True}
            )
            tid = create_resp.json()["template_id"]
            client.delete(f"/api/v1/agent-templates/{tid}")
            mock_del.assert_called_once_with(tid)


# ---------------------------------------------------------------------------
# POST /agents/from-template
# ---------------------------------------------------------------------------


class TestSpawnFromTemplate:
    @patch("obscura.routes.agents.get_runtime")
    def test_spawn_basic(self, mock_get_runtime: Any, client: TestClient) -> None:
        agent = _make_mock_agent()
        runtime = _make_mock_runtime(agent)
        mock_get_runtime.return_value = runtime

        create_resp = client.post("/api/v1/agent-templates", json={"name": "basic"})
        tid = create_resp.json()["template_id"]

        resp = client.post(
            "/api/v1/agents/from-template",
            json={"template_id": tid},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "agent-1"
        assert data["mode"] == "loop"
        runtime.spawn.assert_called_once()

    @patch("obscura.routes.agents.get_runtime")
    def test_spawn_with_mcp_config(self, mock_get_runtime: Any, client: TestClient) -> None:
        agent = _make_mock_agent()
        runtime = _make_mock_runtime(agent)
        mock_get_runtime.return_value = runtime

        create_resp = client.post(
            "/api/v1/agent-templates",
            json={
                "name": "mcp-agent",
                "mcp_servers": [
                    {"name": "pw", "transport": "stdio", "command": "npx", "args": ["-y", "@playwright/mcp"]},
                ],
            },
        )
        tid = create_resp.json()["template_id"]

        client.post("/api/v1/agents/from-template", json={"template_id": tid})

        spawn_kwargs = runtime.spawn.call_args.kwargs
        assert spawn_kwargs["mcp"].enabled is True
        assert len(spawn_kwargs["mcp"].servers) == 1

    @patch("obscura.routes.agents.get_runtime")
    def test_spawn_with_skills_in_prompt(self, mock_get_runtime: Any, client: TestClient) -> None:
        agent = _make_mock_agent()
        runtime = _make_mock_runtime(agent)
        mock_get_runtime.return_value = runtime

        create_resp = client.post(
            "/api/v1/agent-templates",
            json={
                "name": "skilled",
                "system_prompt": "Base prompt.",
                "skills": [{"name": "review", "content": "Check for bugs."}],
            },
        )
        tid = create_resp.json()["template_id"]

        client.post("/api/v1/agents/from-template", json={"template_id": tid})

        spawn_kwargs = runtime.spawn.call_args.kwargs
        prompt: str = spawn_kwargs["system_prompt"]
        assert "Base prompt." in prompt
        assert "## Loaded Skills" in prompt
        assert "Check for bugs." in prompt

    @patch("obscura.routes.agents.get_runtime")
    def test_spawn_not_found(self, mock_get_runtime: Any, client: TestClient) -> None:
        mock_get_runtime.return_value = _make_mock_runtime()
        resp = client.post(
            "/api/v1/agents/from-template",
            json={"template_id": "nonexistent"},
        )
        assert resp.status_code == 404
