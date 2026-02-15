"""Tests for sdk.routes.sessions — Session management endpoints."""
import pytest
from unittest.mock import AsyncMock
from starlette.testclient import TestClient
from sdk.config import ObscuraConfig
from sdk.internal.types import Backend, SessionRef


@pytest.fixture
def app():
    config = ObscuraConfig(auth_enabled=False, otel_enabled=False)
    from sdk.server import create_app
    return create_app(config)


@pytest.fixture
def client(app):
    return TestClient(app)


class TestSessionCreate:
    def test_create_session(self, app, client):
        mock_client = AsyncMock()
        mock_client.create_session.return_value = SessionRef(
            session_id="sess-1", backend=Backend.COPILOT
        )
        mock_client.stop = AsyncMock()
        mock_factory = AsyncMock()
        mock_factory.create.return_value = mock_client
        app.state.client_factory = mock_factory

        resp = client.post("/api/v1/sessions", json={
            "backend": "copilot",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "sess-1"
        assert data["backend"] == "copilot"


class TestSessionList:
    def test_list_sessions(self, app, client):
        mock_client = AsyncMock()
        mock_client.list_sessions.return_value = [
            SessionRef(session_id="s1", backend=Backend.COPILOT),
        ]
        mock_client.stop = AsyncMock()
        mock_factory = AsyncMock()
        mock_factory.create.return_value = mock_client
        app.state.client_factory = mock_factory

        resp = client.get("/api/v1/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1

    def test_list_sessions_backend_error(self, app, client):
        mock_factory = AsyncMock()
        mock_factory.create.side_effect = RuntimeError("no backend")
        app.state.client_factory = mock_factory

        resp = client.get("/api/v1/sessions")
        assert resp.status_code == 200
        # Should return empty list on errors
        assert resp.json() == []


class TestSessionDelete:
    def test_delete_session(self, app, client):
        mock_client = AsyncMock()
        mock_client.delete_session = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_factory = AsyncMock()
        mock_factory.create.return_value = mock_client
        app.state.client_factory = mock_factory

        resp = client.delete("/api/v1/sessions/sess-1", params={"backend": "copilot"})
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
