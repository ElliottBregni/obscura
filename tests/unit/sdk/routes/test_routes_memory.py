"""Tests for sdk.routes.memory — Memory CRUD endpoints."""

from __future__ import annotations

from typing import Any

import pytest
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


class TestMemoryCRUD:
    def test_set_and_get(self, client: TestClient) -> None:
        client.post("/api/v1/memory/test-ns/key1", json={"value": "hello"})
        resp = client.get("/api/v1/memory/test-ns/key1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["value"] == "hello"

    def test_get_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/memory/nonexistent/nokey")
        assert resp.status_code == 404

    def test_delete_key(self, client: TestClient) -> None:
        client.post("/api/v1/memory/test-ns/delkey", json={"value": "bye"})
        resp = client.delete("/api/v1/memory/test-ns/delkey")
        assert resp.status_code == 200

    def test_list_keys(self, client: TestClient) -> None:
        client.post("/api/v1/memory/list-ns/k1", json={"value": "v1"})
        resp = client.get("/api/v1/memory", params={"namespace": "list-ns"})
        assert resp.status_code == 200


class TestMemoryNamespaces:
    def test_list_namespaces(self, client: TestClient) -> None:
        resp = client.get("/api/v1/memory/namespaces")
        assert resp.status_code == 200

    def test_create_namespace(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/memory/namespaces",
            json={
                "name": "my-ns",
                "description": "A test namespace",
            },
        )
        assert resp.status_code == 200


class TestMemoryStats:
    def test_stats(self, client: TestClient) -> None:
        resp = client.get("/api/v1/memory/stats")
        assert resp.status_code == 200

    def test_search(self, client: TestClient) -> None:
        client.post("/api/v1/memory/search-ns/hello", json={"value": "world"})
        resp = client.get("/api/v1/memory/search", params={"q": "hello"})
        assert resp.status_code == 200


class TestMemoryBulk:
    def test_export(self, client: TestClient) -> None:
        resp = client.get("/api/v1/memory/export")
        assert resp.status_code == 200

    def test_import(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/memory/import",
            json={
                "data": {"import-ns": {"k1": "v1"}},
            },
        )
        assert resp.status_code == 200

    def test_transaction(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/memory/transaction",
            json={
                "operations": [
                    {"op": "set", "namespace": "tx-ns", "key": "tk1", "value": "tv1"},
                ]
            },
        )
        assert resp.status_code == 200
