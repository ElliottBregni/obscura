"""Shared fixtures for the lightrag_memory test suite.

This module never imports the real `lightrag` package. `MockLightRAG`
is a behaviorally-faithful drop-in for `LightRAGAdapter` — it inherits
the public surface so `isinstance(x, LightRAGAdapter)` checks in
product code keep passing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from obscura.lightrag_memory.adapter import (
    GraphExplanation,
    GraphHit,
    LightRAGAdapter,
)

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser


# ---------------------------------------------------------------------------
# MockLightRAG — primary unit-test seam
# ---------------------------------------------------------------------------


@dataclass
class _MockState:
    """Inspectable record of every call to a `MockLightRAG` instance."""

    inserts: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    aquery_calls: list[tuple[str, str, int]] = field(default_factory=list)
    canned_aquery: list[tuple[str, list[GraphHit]]] = field(default_factory=list)
    canned_neighbors: dict[str, GraphExplanation] = field(default_factory=dict)
    next_aquery_raises: BaseException | None = None
    next_aquery_sleep_s: float | None = None
    next_insert_raises: BaseException | None = None
    next_insert_sleep_s: float | None = None
    closed: bool = False


class MockLightRAG(LightRAGAdapter):
    """Drop-in replacement for `LightRAGAdapter` — never imports lightrag."""

    indexable_types: frozenset[str] = frozenset({"fact", "summary", "general"})
    MIN_LENGTH: int = 20

    def __init__(self, *_: Any, **__: Any) -> None:
        self.state = _MockState()
        self._closed = False

    @classmethod
    def for_user(  # type: ignore[override]
        cls,
        user: AuthenticatedUser,
        embedding_fn: Any | None = None,
    ) -> MockLightRAG:
        return cls()

    def insert_safe(  # type: ignore[override]
        self,
        doc_id: str,
        text: str,
        metadata: dict[str, Any],
    ) -> None:
        if len(text) < self.MIN_LENGTH:
            metadata = {**metadata, "_skip_reason": "too_short"}
            self.state.inserts.append((doc_id, text, metadata))
            return
        if self.state.next_insert_sleep_s is not None:
            import time as _time

            _time.sleep(self.state.next_insert_sleep_s)
            self.state.next_insert_sleep_s = None
        if self.state.next_insert_raises is not None:
            self.state.next_insert_raises = None
            return
        self.state.inserts.append((doc_id, text, dict(metadata)))

    def delete_safe(self, doc_id: str) -> None:  # type: ignore[override]
        self.state.deletes.append(doc_id)

    async def aquery(  # type: ignore[override]
        self,
        query: str,
        mode: str = "hybrid",
        top_k: int = 20,
    ) -> list[GraphHit]:
        self.state.aquery_calls.append((query, mode, top_k))
        if self.state.next_aquery_raises is not None:
            exc = self.state.next_aquery_raises
            self.state.next_aquery_raises = None
            raise exc
        if self.state.next_aquery_sleep_s is not None:
            await asyncio.sleep(self.state.next_aquery_sleep_s)
            self.state.next_aquery_sleep_s = None
        for substring, hits in self.state.canned_aquery:
            if substring in query:
                return list(hits)
        return []

    def get_neighbors(  # type: ignore[override]
        self,
        doc_id: str,
        depth: int = 1,
    ) -> GraphExplanation:
        return self.state.canned_neighbors.get(
            doc_id,
            GraphExplanation(entities=[], relations=[], neighbors=[]),
        )

    def close(self) -> None:  # type: ignore[override]
        self._closed = True
        self.state.closed = True

    def set_canned(
        self,
        query_substring: str,
        hits: list[GraphHit],
    ) -> None:
        self.state.canned_aquery.append((query_substring, list(hits)))

    def set_canned_neighbors(
        self,
        doc_id: str,
        explanation: GraphExplanation,
    ) -> None:
        self.state.canned_neighbors[doc_id] = explanation


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_lightrag() -> MockLightRAG:
    """A fresh `MockLightRAG` per test."""
    return MockLightRAG()


@pytest.fixture
def fake_user() -> AuthenticatedUser:
    """Minimal `AuthenticatedUser` matching the project's existing pattern."""
    from obscura.auth.models import AuthenticatedUser

    return AuthenticatedUser(
        user_id="u-lightrag-test",
        email="lr@test.com",
        roles=("admin",),
        org_id="org-1",
        token_type="user",
        raw_token="test",
    )


@pytest.fixture
def fake_decay_config() -> Any:
    """A `DecayConfig` with predictable parameters for deterministic tests."""
    from obscura.vector_memory.decay import DecayConfig, DecayProfile

    profiles = {
        "fact": DecayProfile(half_life_days=10.0, min_score_floor=0.001),
        "summary": DecayProfile(half_life_days=10.0, min_score_floor=0.001),
        "episode": DecayProfile(half_life_days=10.0, min_score_floor=0.001),
        "general": DecayProfile(half_life_days=10.0, min_score_floor=0.001),
        "preference": DecayProfile(immune=True),
    }
    return DecayConfig(profiles=profiles, access_boost_days=0.0)


@pytest.fixture
def hybrid_store(
    tmp_path: Path,
    fake_user: AuthenticatedUser,
    mock_lightrag: MockLightRAG,
    fake_decay_config: Any,
) -> Generator[Any, None, None]:
    """Fully-wired `HybridVectorMemoryStore` backed by SQLite in tmp_path."""
    from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
    from obscura.vector_memory import VectorMemoryStore, simple_embedding
    from obscura.vector_memory.backends import BackendConfig, SQLiteBackend

    VectorMemoryStore.reset_instances()

    config = BackendConfig(user_id=fake_user.user_id, embedding_dim=384)
    backend = SQLiteBackend(config=config, db_path=tmp_path / "vec.db")
    store = HybridVectorMemoryStore(
        fake_user,
        backend=backend,
        embedding_fn=simple_embedding,
        decay_config=fake_decay_config,
        lightrag_adapter=mock_lightrag,
    )
    yield store
    store.close()


@pytest.fixture
def vector_entry_factory(fake_user: AuthenticatedUser) -> Any:
    """Callable that produces `VectorEntry` instances with sane defaults."""
    from obscura.memory import MemoryKey
    from obscura.vector_memory.backends import VectorEntry

    def _make(
        key: str = "k1",
        namespace: str = "default",
        text: str = "hello world",
        memory_type: str = "fact",
        metadata: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        accessed_at: datetime | None = None,
        score: float = 0.0,
    ) -> VectorEntry:
        now = datetime.now(UTC)
        return VectorEntry(
            key=MemoryKey(namespace=namespace, key=key),
            text=text,
            embedding=[0.0] * 384,
            metadata=metadata or {},
            memory_type=memory_type,
            created_at=created_at or now,
            updated_at=None,
            accessed_at=accessed_at,
            score=score,
        )

    return _make
