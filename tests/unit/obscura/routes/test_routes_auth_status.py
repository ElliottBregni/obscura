"""Tests for auth status routes."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from obscura.core.config import ObscuraConfig


@pytest.fixture
def app() -> Any:
    config = ObscuraConfig(otel_enabled=False)
    from obscura.server import create_app

    return create_app(config)


@pytest.fixture
def client(app: Any) -> TestClient:
    return TestClient(app, headers={"X-API-Key": "test-api-key"})


def test_provider_secrets_sync_calls_shared_helper(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")

    with patch("obscura.routes.auth_status._sync_provider_secrets_to_supabase") as sync_mock:
        resp = client.post(
            "/api/v1/auth/provider-secrets/sync",
            json={
                "provider": "github",
                "provider_token": "ghp_xxx",
                "provider_refresh_token": "ghr_xxx",
            },
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["provider"] == "github"
    sync_mock.assert_called_once()


def test_provider_secrets_sync_requires_supabase_config(
    client: TestClient,
) -> None:
    with patch("obscura.routes.auth_status.SupabaseCliConfig.from_env", return_value=None):
        resp = client.post(
            "/api/v1/auth/provider-secrets/sync",
            json={
                "provider": "github",
                "provider_token": "ghp_xxx",
            },
        )

    assert resp.status_code == 503
    assert "Supabase is not configured" in resp.json()["detail"]


def test_auth_session_endpoint(client: TestClient) -> None:
    with (
        patch("obscura.routes.auth_status.load_session") as load_session_mock,
        patch("obscura.routes.auth_status.get_access_token", return_value="acc"),
        patch("obscura.routes.auth_status.get_github_token", return_value="ghp_xxx"),
    ):
        load_session_mock.return_value = type("S", (), {"provider": "github", "expires_at": 123})()
        resp = client.get("/api/v1/auth/session")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["authenticated"] is True
    assert payload["provider"] == "github"
    assert payload["github_oauth"] is True


def test_auth_logout_endpoint(client: TestClient) -> None:
    with patch("obscura.routes.auth_status.clear_session", return_value=True) as clear_mock:
        resp = client.post("/api/v1/auth/logout", json={"provider": "github"})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["removed"] is True
    clear_mock.assert_called_once()
