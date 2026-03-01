"""Tests for the /api/v1/models route."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient
from obscura.core.config import ObscuraConfig


@pytest.fixture
def app() -> Any:
    config = ObscuraConfig(auth_enabled=False, otel_enabled=False)
    from obscura.server import create_app

    return create_app(config)


@pytest.fixture
def client(app: Any) -> TestClient:
    return TestClient(app)


class TestListModels:
    def test_list_all_providers(self, client: TestClient) -> None:
        resp = client.get("/api/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert "models" in data
        # Should have entries for known providers
        assert "claude" in data["models"]
        assert isinstance(data["models"]["claude"], list)

    def test_list_claude_models(self, client: TestClient) -> None:
        resp = client.get("/api/v1/models?provider=claude")
        assert resp.status_code == 200
        data = resp.json()
        models = data["models"]["claude"]
        assert len(models) >= 3
        model_ids = [m["id"] for m in models]
        assert "claude-opus-4-6" in model_ids
        assert "claude-sonnet-4-6" in model_ids
        assert "claude-haiku-4-5-20251001" in model_ids

    def test_unknown_provider(self, client: TestClient) -> None:
        resp = client.get("/api/v1/models?provider=nonexistent")
        assert resp.status_code == 400
        assert "Unknown provider" in resp.json()["detail"]

    def test_model_fields(self, client: TestClient) -> None:
        resp = client.get("/api/v1/models?provider=claude")
        data = resp.json()
        model = data["models"]["claude"][0]
        assert "id" in model
        assert "name" in model
        assert "provider" in model
        assert "supports_tools" in model
        assert "supports_vision" in model
        assert "deprecated" in model


class TestRefreshModels:
    def test_refresh_all(self, client: TestClient) -> None:
        resp = client.post("/api/v1/models/refresh")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cache_invalidated"
        assert data["provider"] == "all"

    def test_refresh_single_provider(self, client: TestClient) -> None:
        resp = client.post("/api/v1/models/refresh?provider=claude")
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "claude"

    def test_refresh_unknown_provider(self, client: TestClient) -> None:
        resp = client.post("/api/v1/models/refresh?provider=nonexistent")
        assert resp.status_code == 400
