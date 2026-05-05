"""Tests for the vector-memory data layer (factory, retry, healthcheck, errors).

Backend integration tests that need a running Qdrant / pgvector live in
``tests/integration``; this file mocks the underlying client so we can
exercise the factory + retry/error paths in isolation.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from obscura.data.vector_memory import (
    VectorBackendUnavailable,
    VectorMemoryDisabled,
    VectorMemoryError,
    VectorMemoryRepo,
    VectorRecord,
    vector_healthcheck,
)
from obscura.data.vector_memory._retry import with_retry
from obscura.data.vector_memory.errors import VectorRetryExhausted
from obscura.data.vector_memory.factory import (
    is_vector_memory_enabled,
    resolve_vector_backend,
)


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackendResolution:
    def test_default_is_qdrant(self) -> None:
        with patch.dict(os.environ, {}, clear=False) as _env:
            _env.pop("OBSCURA_VECTOR_BACKEND", None)
            assert resolve_vector_backend() == "qdrant"

    def test_pgvector_explicit(self) -> None:
        with patch.dict(os.environ, {"OBSCURA_VECTOR_BACKEND": "pgvector"}):
            assert resolve_vector_backend() == "pgvector"

    def test_sqlite_vss_explicit(self) -> None:
        with patch.dict(os.environ, {"OBSCURA_VECTOR_BACKEND": "sqlite-vss"}):
            assert resolve_vector_backend() == "sqlite-vss"

    def test_unknown_backend_fails_loud(self) -> None:
        with patch.dict(os.environ, {"OBSCURA_VECTOR_BACKEND": "mongo"}):
            with pytest.raises(
                VectorMemoryError, match="Unknown OBSCURA_VECTOR_BACKEND"
            ):
                resolve_vector_backend()

    def test_disabled_via_env(self) -> None:
        with patch.dict(os.environ, {"OBSCURA_VECTOR_MEMORY": "off"}):
            assert is_vector_memory_enabled() is False

    def test_enabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False) as _env:
            _env.pop("OBSCURA_VECTOR_MEMORY", None)
            assert is_vector_memory_enabled() is True


# ---------------------------------------------------------------------------
# Factory fail-loud behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFactoryFailsLoud:
    def test_disabled_raises(self) -> None:
        from obscura.data.vector_memory.factory import get_vector_memory_repo

        with patch.dict(os.environ, {"OBSCURA_VECTOR_MEMORY": "off"}):
            with pytest.raises(VectorMemoryDisabled):
                get_vector_memory_repo(user_id="test", embedding_dim=4)

    def test_qdrant_init_failure_raises_structured(self) -> None:
        # Force the underlying QdrantBackend to raise on construction.
        with patch(
            "obscura.data.vector_memory.qdrant.QdrantBackend",
            side_effect=RuntimeError("connection refused"),
        ):
            with patch.dict(
                os.environ,
                {"OBSCURA_VECTOR_BACKEND": "qdrant"},
            ):
                from obscura.data.vector_memory.factory import (
                    get_vector_memory_repo,
                )

                with pytest.raises(VectorBackendUnavailable) as ei:
                    get_vector_memory_repo(user_id="test", embedding_dim=4)
                assert ei.value.backend == "qdrant"
                assert "connection refused" in str(ei.value)


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRetry:
    def test_succeeds_first_try(self) -> None:
        calls: list[int] = []

        def f() -> int:
            calls.append(1)
            return 42

        assert with_retry("test.op", f) == 42
        assert len(calls) == 1

    def test_succeeds_after_one_failure(self) -> None:
        calls: list[int] = []

        def f() -> str:
            calls.append(1)
            if len(calls) == 1:
                msg = "transient"
                raise RuntimeError(msg)
            return "ok"

        assert with_retry("test.op", f, base_delay=0.001, max_delay=0.005) == "ok"
        assert len(calls) == 2

    def test_exhaustion_raises_structured(self) -> None:
        def f() -> str:
            msg = "always broken"
            raise ConnectionError(msg)

        with pytest.raises(VectorRetryExhausted) as ei:
            with_retry("test.op", f, attempts=3, base_delay=0.001, max_delay=0.005)
        assert ei.value.op == "test.op"
        assert ei.value.attempts == 3
        assert isinstance(ei.value.cause, ConnectionError)

    def test_non_matching_exception_not_retried(self) -> None:
        calls: list[int] = []

        def f() -> str:
            calls.append(1)
            msg = "config error"
            raise ValueError(msg)

        with pytest.raises(ValueError, match="config error"):
            with_retry(
                "test.op",
                f,
                attempts=5,
                base_delay=0.001,
                retry_on=(ConnectionError,),
            )
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHealthcheck:
    def test_disabled_returns_disabled(self) -> None:
        with patch.dict(os.environ, {"OBSCURA_VECTOR_MEMORY": "off"}):
            result = vector_healthcheck()
            assert result["ok"] is False
            assert result["enabled"] is False
            assert result["backend"] is None

    def test_unknown_backend_returns_error(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OBSCURA_VECTOR_MEMORY": "on",
                "OBSCURA_VECTOR_BACKEND": "mongo",
            },
        ):
            result = vector_healthcheck()
            assert result["ok"] is False
            assert "Unknown" in (result["error"] or "")

    def test_unavailable_backend_returns_error(self) -> None:
        with patch(
            "obscura.data.vector_memory.qdrant.QdrantBackend",
            side_effect=RuntimeError("boom"),
        ):
            with patch.dict(
                os.environ,
                {
                    "OBSCURA_VECTOR_MEMORY": "on",
                    "OBSCURA_VECTOR_BACKEND": "qdrant",
                },
            ):
                result = vector_healthcheck()
                assert result["ok"] is False
                assert result["backend"] == "qdrant"
                assert result["enabled"] is True
                assert "boom" in (result["error"] or "")


# ---------------------------------------------------------------------------
# Adapter shape — verify Protocol conformance without a real backend
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAdapterProtocol:
    def test_qdrant_repo_class_has_required_methods(self) -> None:
        from obscura.data.vector_memory.qdrant import QdrantVectorRepo

        for method in (
            "upsert",
            "search",
            "payload_filter",
            "delete",
            "count",
            "healthcheck",
            "close",
        ):
            assert hasattr(QdrantVectorRepo, method), method

    def test_pgvector_repo_class_has_required_methods(self) -> None:
        from obscura.data.vector_memory.pgvector import PgvectorVectorRepo

        for method in (
            "upsert",
            "search",
            "payload_filter",
            "delete",
            "count",
            "healthcheck",
            "close",
        ):
            assert hasattr(PgvectorVectorRepo, method), method

    def test_sqlite_vss_repo_class_has_required_methods(self) -> None:
        from obscura.data.vector_memory.sqlite_vss import SqliteVssVectorRepo

        for method in (
            "upsert",
            "search",
            "payload_filter",
            "delete",
            "count",
            "healthcheck",
            "close",
        ):
            assert hasattr(SqliteVssVectorRepo, method), method


# ---------------------------------------------------------------------------
# Adapter delegation — mock the underlying VectorBackend
# ---------------------------------------------------------------------------


def _fake_entry(namespace: str, key: str, score: float = 0.0) -> Any:
    """Build a minimal VectorEntry-shaped object for adapter tests."""
    e = MagicMock()
    e.key.namespace = namespace
    e.key.key = key
    e.text = f"{namespace}/{key} text"
    e.embedding = [0.1, 0.2, 0.3, 0.4]
    e.metadata = {"memory_type": "general"}
    e.score = score
    e.final_score = score
    return e


@pytest.mark.unit
class TestAdapterDelegation:
    def test_search_translates_results(self) -> None:
        from obscura.data.vector_memory._legacy_adapter import LegacyBackendAdapter

        backend = MagicMock()
        backend.search_vectors.return_value = [
            _fake_entry("user:profile", "elliott", score=0.9),
            _fake_entry("user:prefs", "terse", score=0.7),
        ]
        adapter = LegacyBackendAdapter(backend=backend, name="test")
        results = adapter.search([0.1, 0.2, 0.3, 0.4], top_k=5)
        assert len(results) == 2
        assert all(isinstance(r, VectorRecord) for r in results)
        assert results[0].namespace == "user:profile"
        assert results[0].score == 0.9

    def test_upsert_rejects_empty_embedding(self) -> None:
        from obscura.data.vector_memory._legacy_adapter import LegacyBackendAdapter
        from obscura.data.vector_memory.errors import VectorPayloadError

        backend = MagicMock()
        adapter = LegacyBackendAdapter(backend=backend, name="test")
        with pytest.raises(VectorPayloadError):
            adapter.upsert([VectorRecord(namespace="a", key="b", text="t")])

    def test_search_rejects_empty_query_embedding(self) -> None:
        from obscura.data.vector_memory._legacy_adapter import LegacyBackendAdapter
        from obscura.data.vector_memory.errors import VectorPayloadError

        backend = MagicMock()
        adapter = LegacyBackendAdapter(backend=backend, name="test")
        with pytest.raises(VectorPayloadError):
            adapter.search([], top_k=3)

    def test_healthcheck_returns_true_on_stats_success(self) -> None:
        from obscura.data.vector_memory._legacy_adapter import LegacyBackendAdapter

        backend = MagicMock()
        backend.get_stats.return_value = {"count": 10}
        adapter = LegacyBackendAdapter(backend=backend, name="test")
        assert adapter.healthcheck() is True

    def test_healthcheck_returns_false_on_stats_failure(self) -> None:
        from obscura.data.vector_memory._legacy_adapter import LegacyBackendAdapter

        backend = MagicMock()
        backend.get_stats.side_effect = ConnectionError("down")
        adapter = LegacyBackendAdapter(backend=backend, name="test")
        assert adapter.healthcheck() is False


@pytest.mark.unit
class TestVectorRecord:
    def test_to_dict_round_trip(self) -> None:
        r = VectorRecord(
            namespace="user:profile",
            key="elliott",
            text="hello",
            embedding=[0.1, 0.2],
            metadata={"role": "engineer"},
            score=0.42,
        )
        d = r.to_dict()
        assert d["namespace"] == "user:profile"
        assert d["score"] == 0.42
        assert d["metadata"] == {"role": "engineer"}
        # Embedding intentionally omitted — it's not useful in tool output
        assert "embedding" not in d


@pytest.mark.unit
class TestProtocolRuntimeCheck:
    def test_protocol_is_runtime_checkable(self) -> None:
        # Sanity: a class with all required methods should pass isinstance().
        from obscura.data.vector_memory._legacy_adapter import LegacyBackendAdapter

        backend = MagicMock()
        adapter = LegacyBackendAdapter(backend=backend, name="test")
        assert isinstance(adapter, VectorMemoryRepo)
