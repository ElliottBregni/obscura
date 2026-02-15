"""Tests for sdk.routes.memory — Memory CRUD endpoints."""

import pytest
from starlette.testclient import TestClient
from sdk.config import ObscuraConfig


@pytest.fixture
def app():
    config = ObscuraConfig(auth_enabled=False, otel_enabled=False)
    from sdk.server import create_app

    return create_app(config)


@pytest.fixture
def client(app):
    return TestClient(app)


class TestMemoryCRUD:
    def test_set_and_get(self, client):
        client.post("/api/v1/memory/test-ns/key1", json={"value": "hello"})
        resp = client.get("/api/v1/memory/test-ns/key1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["value"] == "hello"

    def test_get_not_found(self, client):
        resp = client.get("/api/v1/memory/nonexistent/nokey")
        assert resp.status_code == 404

    def test_delete_key(self, client):
        client.post("/api/v1/memory/test-ns/delkey", json={"value": "bye"})
        resp = client.delete("/api/v1/memory/test-ns/delkey")
        assert resp.status_code == 200

    def test_list_keys(self, client):
        client.post("/api/v1/memory/list-ns/k1", json={"value": "v1"})
        resp = client.get("/api/v1/memory", params={"namespace": "list-ns"})
        assert resp.status_code == 200


class TestMemoryNamespaces:
    def test_list_namespaces(self, client):
        resp = client.get("/api/v1/memory/namespaces")
        assert resp.status_code == 200

    def test_create_namespace(self, client):
        resp = client.post(
            "/api/v1/memory/namespaces",
            json={
                "name": "my-ns",
                "description": "A test namespace",
            },
        )
        assert resp.status_code == 200


class TestMemoryStats:
    def test_stats(self, client):
        resp = client.get("/api/v1/memory/stats")
        assert resp.status_code == 200

    def test_search(self, client):
        client.post("/api/v1/memory/search-ns/hello", json={"value": "world"})
        resp = client.get("/api/v1/memory/search", params={"q": "hello"})
        assert resp.status_code == 200


class TestMemoryBulk:
    def test_export(self, client):
        resp = client.get("/api/v1/memory/export")
        assert resp.status_code == 200

    def test_import(self, client):
        resp = client.post(
            "/api/v1/memory/import",
            json={
                "data": {"import-ns": {"k1": "v1"}},
            },
        )
        assert resp.status_code == 200

    def test_transaction(self, client):
        resp = client.post(
            "/api/v1/memory/transaction",
            json={
                "operations": [
                    {"op": "set", "namespace": "tx-ns", "key": "tk1", "value": "tv1"},
                ]
            },
        )
        assert resp.status_code == 200
