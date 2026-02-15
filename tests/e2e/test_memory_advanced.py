"""E2E Tests: Advanced Memory Features (Phase 2)."""

import pytest


@pytest.mark.e2e
class TestMemoryNamespaces:
    """Test memory namespace management."""

    def test_list_namespaces(self, client):
        """Can list all memory namespaces."""
        # Create some data in different namespaces
        client.post("/api/v1/memory/session/key1", json={"value": "data1"})
        client.post("/api/v1/memory/project/key2", json={"value": "data2"})

        resp = client.get("/api/v1/memory/namespaces")

        assert resp.status_code == 200
        data = resp.json()
        assert "namespaces" in data
        assert "session" in data["namespaces"]
        assert "project" in data["namespaces"]

    def test_create_namespace(self, client):
        """Can create a memory namespace."""
        resp = client.post(
            "/api/v1/memory/namespaces",
            json={
                "name": "my-custom-namespace",
                "description": "Custom namespace for testing",
                "ttl_days": 30,
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["namespace_id"] == "my-custom-namespace"
        assert data["description"] == "Custom namespace for testing"
        assert data["ttl_days"] == 30

    def test_delete_namespace(self, client):
        """Can delete a memory namespace."""
        # Create namespace
        client.post("/api/v1/memory/namespaces", json={"name": "temp-namespace"})

        # Delete without deleting data
        resp = client.delete(
            "/api/v1/memory/namespaces/temp-namespace", params={"delete_data": "false"}
        )

        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert resp.json()["keys_deleted"] == 0

    def test_delete_namespace_with_data(self, client):
        """Can delete namespace and all its data."""
        # Create namespace and data
        client.post("/api/v1/memory/namespaces", json={"name": "temp-with-data"})
        client.post("/api/v1/memory/temp-with-data/key1", json={"value": "data1"})
        client.post("/api/v1/memory/temp-with-data/key2", json={"value": "data2"})

        # Delete with data
        resp = client.delete(
            "/api/v1/memory/namespaces/temp-with-data", params={"delete_data": "true"}
        )

        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert resp.json()["keys_deleted"] == 2

    def test_namespace_stats(self, client):
        """Can get namespace statistics."""
        # Create data
        client.post("/api/v1/memory/stats-test-ns/key1", json={"value": "x" * 100})
        client.post("/api/v1/memory/stats-test-ns/key2", json={"value": "y" * 50})

        resp = client.get("/api/v1/memory/namespaces/stats-test-ns/stats")

        assert resp.status_code == 200
        data = resp.json()
        assert data["namespace"] == "stats-test-ns"
        assert data["key_count"] == 2
        assert data["total_size_bytes"] > 0


@pytest.mark.e2e
class TestMemoryTransactions:
    """Test memory transaction operations."""

    def test_transaction_set_and_get(self, client):
        """Can execute set and get operations in transaction."""
        resp = client.post(
            "/api/v1/memory/transaction",
            json={
                "operations": [
                    {
                        "op": "set",
                        "namespace": "txn-test",
                        "key": "key1",
                        "value": "value1",
                    },
                    {"op": "get", "namespace": "txn-test", "key": "key1"},
                    {
                        "op": "set",
                        "namespace": "txn-test",
                        "key": "key2",
                        "value": {"nested": "data"},
                    },
                ]
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["successful"] == 3
        assert data["results"][1]["value"] == "value1"

    def test_transaction_delete(self, client):
        """Can delete keys in transaction."""
        # Set up data
        client.post("/api/v1/memory/txn-del-test/key1", json={"value": "to-delete"})

        resp = client.post(
            "/api/v1/memory/transaction",
            json={
                "operations": [
                    {"op": "delete", "namespace": "txn-del-test", "key": "key1"},
                    {"op": "get", "namespace": "txn-del-test", "key": "key1"},
                ]
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["results"][0]["deleted"] is True
        assert data["results"][1]["value"] is None

    def test_transaction_empty_operations(self, client):
        """Transaction with empty operations returns error."""
        resp = client.post("/api/v1/memory/transaction", json={"operations": []})

        assert resp.status_code == 400
        assert "No operations provided" in resp.json()["detail"]

    def test_transaction_mixed_namespaces(self, client):
        """Can operate on multiple namespaces in one transaction."""
        resp = client.post(
            "/api/v1/memory/transaction",
            json={
                "operations": [
                    {"op": "set", "namespace": "ns1", "key": "k1", "value": "v1"},
                    {"op": "set", "namespace": "ns2", "key": "k2", "value": "v2"},
                    {"op": "get", "namespace": "ns1", "key": "k1"},
                    {"op": "get", "namespace": "ns2", "key": "k2"},
                ]
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["successful"] == 4
        assert data["results"][2]["value"] == "v1"
        assert data["results"][3]["value"] == "v2"


@pytest.mark.e2e
class TestMemoryImportExport:
    """Test memory import and export."""

    def test_export_all_memory(self, client):
        """Can export all memory data."""
        # Create test data
        client.post("/api/v1/memory/export-ns1/key1", json={"value": "data1"})
        client.post("/api/v1/memory/export-ns2/key2", json={"value": "data2"})

        resp = client.get("/api/v1/memory/export")

        assert resp.status_code == 200
        data = resp.json()
        assert "exported_at" in data
        assert "export-ns1" in data["data"]
        assert "export-ns2" in data["data"]
        assert data["data"]["export-ns1"]["key1"] == "data1"
        assert data["total_keys"] >= 2

    def test_export_single_namespace(self, client):
        """Can export single namespace."""
        # Create test data
        client.post("/api/v1/memory/export-single/key1", json={"value": "ns-data"})
        client.post("/api/v1/memory/other-ns/key2", json={"value": "other-data"})

        resp = client.get("/api/v1/memory/export?namespace=export-single")

        assert resp.status_code == 200
        data = resp.json()
        assert list(data["data"].keys()) == ["export-single"]
        assert data["data"]["export-single"]["key1"] == "ns-data"

    def test_import_memory(self, client):
        """Can import memory data."""
        import_data = {
            "import-test-ns": {
                "key1": "imported-value-1",
                "key2": {"nested": "imported"},
            }
        }

        resp = client.post("/api/v1/memory/import", json={"data": import_data})

        assert resp.status_code == 200
        data = resp.json()
        assert data["imported"] == 2
        assert data["skipped"] == 0

        # Verify data was imported
        get_resp = client.get("/api/v1/memory/import-test-ns/key1")
        assert get_resp.json()["value"] == "imported-value-1"

    def test_import_with_overwrite(self, client):
        """Import can overwrite existing keys."""
        # Set initial value
        client.post("/api/v1/memory/import-overwrite/key1", json={"value": "original"})

        import_data = {"import-overwrite": {"key1": "overwritten"}}

        resp = client.post(
            "/api/v1/memory/import?overwrite=true", json={"data": import_data}
        )

        assert resp.status_code == 200
        assert resp.json()["imported"] == 1

        # Verify overwrite
        get_resp = client.get("/api/v1/memory/import-overwrite/key1")
        assert get_resp.json()["value"] == "overwritten"

    def test_import_without_overwrite(self, client):
        """Import can skip existing keys."""
        # Set initial value
        client.post("/api/v1/memory/import-skip/key1", json={"value": "original"})

        import_data = {"import-skip": {"key1": "new-value"}}

        resp = client.post(
            "/api/v1/memory/import?overwrite=false", json={"data": import_data}
        )

        assert resp.status_code == 200
        assert resp.json()["imported"] == 0
        assert resp.json()["skipped"] == 1

        # Verify original preserved
        get_resp = client.get("/api/v1/memory/import-skip/key1")
        assert get_resp.json()["value"] == "original"

    def test_import_empty_data(self, client):
        """Import with empty data returns error."""
        resp = client.post("/api/v1/memory/import", json={"data": {}})

        assert resp.status_code == 400
        assert "No data provided" in resp.json()["detail"]

    def test_roundtrip_export_import(self, client):
        """Can export and re-import data."""
        # Create data
        client.post("/api/v1/memory/roundtrip/key1", json={"value": "test-data"})
        client.post("/api/v1/memory/roundtrip/key2", json={"value": [1, 2, 3]})

        # Export
        export_resp = client.get("/api/v1/memory/export?namespace=roundtrip")
        export_data = export_resp.json()["data"]

        # Clear data
        client.delete(
            "/api/v1/memory/namespaces/roundtrip", params={"delete_data": "true"}
        )

        # Re-import
        import_resp = client.post("/api/v1/memory/import", json={"data": export_data})

        assert import_resp.status_code == 200
        assert import_resp.json()["imported"] == 2

        # Verify
        get_resp = client.get("/api/v1/memory/roundtrip/key1")
        assert get_resp.json()["value"] == "test-data"
