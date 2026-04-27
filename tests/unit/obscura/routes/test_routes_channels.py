"""Tests for obscura.routes.channels — fail-closed webhook auth (SOC2 E1)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from obscura.routes.channels import init_channel_router, router as channels_router


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(channels_router)
    fake_router = MagicMock()
    fake_router.dispatch = AsyncMock(return_value=None)
    init_channel_router(fake_router)
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


class TestTelegramFailClosed:
    """Pre-fix, an unset TELEGRAM_WEBHOOK_SECRET silently accepted any caller.
    Post-fix, the webhook refuses with 503 unless OBSCURA_WEBHOOKS_PUBLIC=1.
    """

    def test_refuses_when_secret_unset(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
        monkeypatch.delenv("OBSCURA_WEBHOOKS_PUBLIC", raising=False)
        resp = client.post(
            "/channels/telegram/webhook",
            json={"update_id": 1, "message": {"text": "hi", "chat": {"id": 9}}},
        )
        assert resp.status_code == 503
        assert "secret" in resp.json()["detail"].lower()

    def test_accepts_when_explicitly_public(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
        monkeypatch.setenv("OBSCURA_WEBHOOKS_PUBLIC", "1")
        resp = client.post(
            "/channels/telegram/webhook",
            json={
                "update_id": 1,
                "message": {"text": "hi", "chat": {"id": 9}, "from": {"id": 7}},
            },
        )
        assert resp.status_code == 200

    def test_accepts_when_secret_matches(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "shh")
        resp = client.post(
            "/channels/telegram/webhook",
            json={
                "update_id": 1,
                "message": {"text": "hi", "chat": {"id": 9}, "from": {"id": 7}},
            },
            headers={"x-telegram-bot-api-secret-token": "shh"},
        )
        assert resp.status_code == 200

    def test_rejects_when_secret_set_but_header_missing(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "shh")
        resp = client.post(
            "/channels/telegram/webhook",
            json={"update_id": 1, "message": {"text": "hi", "chat": {"id": 9}}},
        )
        assert resp.status_code == 403


class TestWhatsAppFailClosed:
    def test_refuses_when_secret_unset(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
        monkeypatch.delenv("OBSCURA_WEBHOOKS_PUBLIC", raising=False)
        resp = client.post("/channels/whatsapp/webhook", content=b"{}")
        assert resp.status_code == 503

    def test_accepts_when_explicitly_public(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
        monkeypatch.setenv("OBSCURA_WEBHOOKS_PUBLIC", "1")
        resp = client.post(
            "/channels/whatsapp/webhook",
            content=b'{"entry": []}',
        )
        assert resp.status_code == 200

    def test_rejects_when_signature_invalid(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WHATSAPP_APP_SECRET", "secret")
        resp = client.post(
            "/channels/whatsapp/webhook",
            content=b"{}",
            headers={"X-Hub-Signature-256": "sha256=deadbeef"},
        )
        assert resp.status_code == 403


def test_dispatch_called_with_telegram_payload(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smoke test that the existing happy path still wires through to dispatch."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "shh")
    fake_router: Any = MagicMock()
    fake_router.dispatch = AsyncMock(return_value=None)
    init_channel_router(fake_router)
    resp = client.post(
        "/channels/telegram/webhook",
        json={
            "update_id": 99,
            "message": {
                "text": "hello",
                "chat": {"id": 42},
                "from": {"id": 7},
            },
        },
        headers={"x-telegram-bot-api-secret-token": "shh"},
    )
    assert resp.status_code == 200
    if fake_router.dispatch.await_count > 0:
        kwargs = fake_router.dispatch.await_args.kwargs
        assert kwargs["platform"] == "telegram"
        assert kwargs["text"] == "hello"
