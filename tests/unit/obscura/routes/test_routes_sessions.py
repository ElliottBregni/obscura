"""Tests for sdk.routes.sessions — Session management endpoints."""

from __future__ import annotations

from typing import Any

import pytest
from unittest.mock import AsyncMock, patch
from starlette.testclient import TestClient
from obscura.core.config import ObscuraConfig
from obscura.core.types import Backend, SessionRef


@pytest.fixture
def app() -> Any:
    config = ObscuraConfig(auth_enabled=False, otel_enabled=False)
    from obscura.server import create_app

    return create_app(config)


@pytest.fixture
def client(app: Any) -> TestClient:
    return TestClient(app)


class TestSessionCreate:
    @patch("obscura.routes.sessions.sync_session_lifecycle")
    @patch("obscura.routes.sessions.broadcast_event", new_callable=AsyncMock)
    def test_create_session(
        self,
        mock_broadcast: AsyncMock,
        mock_sync: AsyncMock,
        app: Any,
        client: TestClient,
    ) -> None:
        mock_client: Any = AsyncMock()
        mock_client.create_session.return_value = SessionRef(
            session_id="sess-1", backend=Backend.COPILOT
        )
        mock_client.stop = AsyncMock()
        mock_factory: Any = AsyncMock()
        mock_factory.create.return_value = mock_client
        app.state.client_factory = mock_factory

        resp = client.post(
            "/api/v1/sessions",
            json={
                "backend": "copilot",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "sess-1"
        assert data["backend"] == "copilot"
        assert data["source"] == "live"
        mock_sync.assert_called_once()
        mock_broadcast.assert_awaited_once()


class TestSessionList:
    def test_list_sessions(self, app: Any, client: TestClient) -> None:
        mock_client: Any = AsyncMock()
        mock_client.list_sessions.return_value = [
            SessionRef(session_id="s1", backend=Backend.COPILOT),
        ]
        mock_client.stop = AsyncMock()
        mock_factory: Any = AsyncMock()
        mock_factory.create.return_value = mock_client
        app.state.client_factory = mock_factory

        resp = client.get("/api/v1/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1

    def test_list_sessions_backend_error(self, app: Any, client: TestClient) -> None:
        mock_factory: Any = AsyncMock()
        mock_factory.create.side_effect = RuntimeError("no backend")
        app.state.client_factory = mock_factory

        resp = client.get("/api/v1/sessions")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_list_sessions_includes_ingested_memory(
        self, app: Any, client: TestClient
    ) -> None:
        mock_factory: Any = AsyncMock()
        mock_factory.create.side_effect = RuntimeError("no backend")
        app.state.client_factory = mock_factory

        # Simulate previously ingested session metadata.
        client.post(
            "/api/v1/memory/sessions/codex-session-1",
            json={
                "value": {
                    "id": "codex-session-1",
                    "agent": "codex",
                    "started": "2026-02-22T00:00:00+00:00",
                }
            },
        )

        resp = client.get("/api/v1/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert any(s["session_id"] == "codex-session-1" for s in data)
        assert any(s["backend"] == "codex" for s in data)
        assert any(
            s["session_id"] == "codex-session-1" and s.get("source") == "ingested"
            for s in data
        )


class TestSessionDelete:
    @patch("obscura.routes.sessions.sync_session_lifecycle")
    @patch("obscura.routes.sessions.broadcast_event", new_callable=AsyncMock)
    def test_delete_session(
        self,
        mock_broadcast: AsyncMock,
        mock_sync: AsyncMock,
        app: Any,
        client: TestClient,
    ) -> None:
        mock_client: Any = AsyncMock()
        mock_client.delete_session = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_factory: Any = AsyncMock()
        mock_factory.create.return_value = mock_client
        app.state.client_factory = mock_factory

        resp = client.delete("/api/v1/sessions/sess-1", params={"backend": "copilot"})
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        mock_sync.assert_called_once()
        mock_broadcast.assert_awaited_once()


class TestSessionIngest:
    @patch("obscura.routes.sessions.sync_and_ingest_system_sessions")
    @patch("obscura.routes.sessions.broadcast_event", new_callable=AsyncMock)
    def test_ingest_sessions_success(
        self,
        mock_broadcast: AsyncMock,
        mock_ingest: Any,
        client: TestClient,
    ) -> None:
        mock_ingest.return_value = {
            "synced": True,
            "entries": 3,
            "ingested": 2,
            "skipped": 1,
            "agent": None,
            "force": False,
            "index_path": "/tmp/index.jsonl",
        }

        resp = client.post("/api/v1/sessions/ingest", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["ingested"] == 2
        mock_ingest.assert_called_once()
        mock_broadcast.assert_awaited_once()

    @patch("obscura.routes.sessions.sync_and_ingest_system_sessions")
    @patch("obscura.routes.sessions.broadcast_event", new_callable=AsyncMock)
    def test_ingest_sessions_copy_to_pwd_flags(
        self,
        _mock_broadcast: AsyncMock,
        mock_ingest: Any,
        client: TestClient,
    ) -> None:
        mock_ingest.return_value = {
            "synced": True,
            "entries": 0,
            "ingested": 0,
            "skipped": 0,
            "agent": None,
            "force": False,
            "index_path": "/tmp/index.jsonl",
            "copy_to_pwd": True,
            "copy_result": {
                "source": "/Users/test/.obscura",
                "destination": "/tmp/app/.obscura",
                "overwrite": True,
                "copied": True,
            },
        }

        resp = client.post(
            "/api/v1/sessions/ingest",
            json={"copy_to_pwd": True, "copy_overwrite": True},
        )
        assert resp.status_code == 200
        mock_ingest.assert_called_once()
        _, kwargs = mock_ingest.call_args
        assert kwargs["copy_to_pwd"] is True
        assert kwargs["copy_overwrite"] is True

    def test_ingest_sessions_invalid_agent(self, client: TestClient) -> None:
        resp = client.post("/api/v1/sessions/ingest", json={"agent": "unknown"})
        assert resp.status_code == 400

    @patch("obscura.routes.sessions.preflight_system_session_ingest")
    def test_ingest_sessions_preflight(self, mock_preflight: Any, client: TestClient) -> None:
        mock_preflight.return_value = {
            "ready": True,
            "agent_sync_script_exists": True,
            "obscura_home_exists": True,
            "sessions_root_writable": True,
        }
        resp = client.get("/api/v1/sessions/ingest/preflight")
        assert resp.status_code == 200
        assert resp.json()["ready"] is True
