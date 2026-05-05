"""Shared adapter for the legacy ``VectorBackend`` Protocol.

PostgreSQL (pgvector) and SQLite (sqlite-vss) backends both implement
the same low-level :class:`obscura.vector_memory.backends.base.VectorBackend`
shape, so the new :class:`VectorMemoryRepo` adapter is identical for
both — only the underlying class differs. Keeps ``pgvector.py`` and
``sqlite_vss.py`` thin (they just construct the adapter).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from obscura.data.vector_memory._retry import with_retry
from obscura.data.vector_memory.errors import VectorPayloadError
from obscura.data.vector_memory.protocol import VectorRecord
from obscura.memory.types import MemoryKey

logger = logging.getLogger(__name__)


class LegacyBackendAdapter:
    """Adapt any :class:`VectorBackend` to the new :class:`VectorMemoryRepo`."""

    def __init__(self, *, backend: Any, name: str) -> None:  # noqa: ANN401  # legacy VectorBackend Protocol; intentional Any
        self._backend = backend
        self.backend_name = name

    def upsert(self, records: list[VectorRecord]) -> int:
        if not records:
            return 0
        for r in records:
            if not r.embedding:
                msg = f"upsert: {r.namespace}:{r.key} has empty embedding"
                raise VectorPayloadError(msg)

        def _do() -> int:
            for r in records:
                self._backend.store_vector(
                    key=MemoryKey(namespace=r.namespace, key=r.key),
                    text=r.text,
                    embedding=r.embedding,
                    metadata=r.metadata,
                    memory_type=str(r.metadata.get("memory_type", "general")),
                    expires_at=_extract_expires_at(r.metadata),
                )
            return len(records)

        return with_retry(f"{self.backend_name}.upsert", _do)

    def search(
        self,
        query_embedding: list[float],
        *,
        namespace: str | None = None,
        top_k: int = 5,
        score_threshold: float | None = None,
    ) -> list[VectorRecord]:
        if not query_embedding:
            msg = "search: query_embedding must be non-empty"
            raise VectorPayloadError(msg)

        def _do() -> list[VectorRecord]:
            entries = self._backend.search_vectors(
                query_embedding=query_embedding,
                namespace=namespace,
                top_k=top_k,
                threshold=score_threshold,
                filters=None,
            )
            return [_entry_to_record(e) for e in entries]

        return with_retry(f"{self.backend_name}.search", _do)

    def payload_filter(
        self,
        *,
        namespace: str | None = None,
        metadata: dict[str, Any] | None = None,
        top_k: int = 50,
    ) -> list[VectorRecord]:
        keys = self._backend.list_keys(namespace=namespace)
        out: list[VectorRecord] = []
        wanted = metadata or {}
        for k in keys[: top_k * 4]:
            entry = self._backend.get_vector(k)
            if entry is None:
                continue
            if all(entry.metadata.get(mk) == mv for mk, mv in wanted.items()):
                out.append(_entry_to_record(entry))
                if len(out) >= top_k:
                    break
        return out

    def delete(self, namespace: str, key: str) -> bool:
        return bool(
            with_retry(
                f"{self.backend_name}.delete",
                lambda: self._backend.delete_vector(
                    MemoryKey(namespace=namespace, key=key),
                ),
            ),
        )

    def count(self, *, namespace: str | None = None) -> int:
        if namespace is None:
            stats = self._backend.get_stats()
            return int(stats.get("count", 0))
        return len(self._backend.list_keys(namespace=namespace))

    def healthcheck(self) -> bool:
        try:
            self._backend.get_stats()
        except Exception:
            logger.debug("%s healthcheck failed", self.backend_name, exc_info=True)
            return False
        return True

    def close(self) -> None:
        try:
            self._backend.close()
        except Exception:
            logger.debug("%s close failed", self.backend_name, exc_info=True)


def _entry_to_record(entry: Any) -> VectorRecord:  # noqa: ANN401  # legacy VectorEntry shape
    return VectorRecord(
        namespace=entry.key.namespace,
        key=entry.key.key,
        text=entry.text,
        embedding=list(entry.embedding) if entry.embedding else [],
        metadata=dict(entry.metadata),
        score=float(entry.final_score or entry.score or 0.0),
    )


def _extract_expires_at(metadata: dict[str, Any]) -> datetime | None:
    raw = metadata.get("expires_at")
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=UTC)
        except ValueError:
            logger.debug("invalid expires_at: %r", raw, exc_info=True)
            return None
    return None
