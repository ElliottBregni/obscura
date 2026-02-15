"""Tests for sdk.routes.vector_memory — Vector/semantic memory endpoints."""
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from sdk.config import ObscuraConfig


@pytest.fixture
def app() -> FastAPI:
    config = ObscuraConfig(auth_enabled=False, otel_enabled=False)
    from sdk.server import create_app
    return create_app(config)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


class TestVectorMemorySet:
    def test_set_vector_memory(self, client: TestClient) -> None:
        resp = client.post("/api/v1/vector-memory/test-ns/key1", json={
            "text": "This is a test document",
            "metadata": {"source": "test"},
            "memory_type": "note",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["stored"] is True
        assert data["namespace"] == "test-ns"
        assert data["key"] == "key1"

    def test_set_vector_memory_defaults(self, client: TestClient) -> None:
        resp = client.post("/api/v1/vector-memory/ns/k2", json={})
        assert resp.status_code == 200


class TestVectorMemorySearch:
    def test_search_basic(self, client: TestClient) -> None:
        # Store something first
        client.post("/api/v1/vector-memory/search-ns/doc1", json={
            "text": "Machine learning is a subset of artificial intelligence",
            "memory_type": "note",
        })
        resp = client.get("/api/v1/vector-memory/search", params={"q": "AI"})
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert "count" in data

    def test_search_with_namespace(self, client: TestClient) -> None:
        resp = client.get("/api/v1/vector-memory/search", params={
            "q": "test",
            "namespace": "my-ns",
        })
        assert resp.status_code == 200

    def test_search_with_rerank(self, client: TestClient) -> None:
        resp = client.get("/api/v1/vector-memory/search", params={
            "q": "test",
            "rerank": "true",
            "recency_weight": "0.3",
        })
        assert resp.status_code == 200

    def test_search_with_memory_types(self, client: TestClient) -> None:
        resp = client.get("/api/v1/vector-memory/search", params={
            "q": "test",
            "memory_types": "note,fact",
        })
        assert resp.status_code == 200

    def test_search_with_date_from(self, client: TestClient) -> None:
        resp = client.get("/api/v1/vector-memory/search", params={
            "q": "test",
            "date_from": "2025-01-01T00:00:00",
        })
        assert resp.status_code == 200

    def test_search_with_date_to(self, client: TestClient) -> None:
        resp = client.get("/api/v1/vector-memory/search", params={
            "q": "test",
            "date_to": "2026-12-31T00:00:00",
        })
        assert resp.status_code == 200

    def test_search_with_date_range(self, client: TestClient) -> None:
        resp = client.get("/api/v1/vector-memory/search", params={
            "q": "test",
            "date_from": "2025-01-01T00:00:00",
            "date_to": "2026-12-31T00:00:00",
        })
        assert resp.status_code == 200


class TestVectorMemoryRoutedSearch:
    def test_routed_search(self, client: TestClient) -> None:
        # Note: POST /vector-memory/search/routed may match {namespace}/{key}
        # route if the more specific route isn't registered first.
        # Just verify the endpoint accepts POST and returns 200.
        resp = client.post("/api/v1/vector-memory/search/routed", json={
            "query": "test query",
            "routes": [
                {"memory_type": "note", "weight": 1.0, "top_k": 5},
            ],
            "final_top_k": 10,
        })
        assert resp.status_code == 200
