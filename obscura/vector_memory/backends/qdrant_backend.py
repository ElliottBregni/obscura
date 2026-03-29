"""Qdrant-based vector memory backend."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import logging
import threading
import time

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
    KeywordIndexParams,
)

from obscura.memory import MemoryKey
from obscura.vector_memory.backends.base import BackendConfig, VectorEntry
from obscura.vector_memory.vector_memory_filters import MetadataFilter

logger = logging.getLogger(__name__)


def _point_id(namespace: str, key: str) -> int:
    """Return a stable deterministic 63-bit point ID using SHA-256.

    Python's built-in hash() is randomised per-process (PYTHONHASHSEED),
    which would cause every restart to produce different IDs for the same
    key, making upsert non-idempotent and get_vector always miss.
    SHA-256 is stable across processes, machines, and Python versions.
    """
    digest = hashlib.sha256(f"{namespace}:{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


class QdrantBackend:
    """Qdrant-based vector memory backend."""

    def __init__(
        self,
        config: BackendConfig,
        mode: str = "local",
        path: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
    ):
        self.config = config
        self._db_id = hashlib.sha256(config.user_id.encode()).hexdigest()[:16]
        self.collection_name = f"user_{self._db_id}"

        if mode == "local":
            if path is None:
                path = str(
                    Path(
                        os.environ.get(
                            "OBSCURA_QDRANT_PATH",
                            Path.home() / ".obscura" / "qdrant",
                        ),
                    ),
                )
            self.client = QdrantClient(path=path)
        elif mode == "memory":
            self.client = QdrantClient(":memory:")
        elif mode == "cloud":
            self.client = QdrantClient(
                url=url or os.environ.get("QDRANT_URL", "http://localhost:6333"),
                api_key=api_key or os.environ.get("QDRANT_API_KEY"),
            )
        else:
            raise ValueError(f"Unknown Qdrant mode: {mode}")

        self._init_collection()

    def _init_collection(self) -> None:
        collections = self.client.get_collections().collections
        if self.collection_name not in [c.name for c in collections]:
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(
                    size=self.config.embedding_dim,
                    distance=Distance.COSINE,
                ),
            )
            self.client.create_payload_index(
                self.collection_name,
                "namespace",
                KeywordIndexParams(type="keyword"),
            )
            self.client.create_payload_index(
                self.collection_name,
                "memory_type",
                KeywordIndexParams(type="keyword"),
            )

    def store_vector(
        self,
        key: MemoryKey,
        text: str,
        embedding: list[float],
        metadata: dict[str, Any],
        memory_type: str,
        expires_at: datetime | None,
    ) -> None:
        point_id = _point_id(key.namespace, key.key)
        payload = {
            "namespace": key.namespace,
            "key": key.key,
            "text": text,
            "metadata": metadata,
            "memory_type": memory_type,
            "created_at": datetime.now(UTC).isoformat(),
            # Embedding provenance (best-effort from env vars)
            "embedding_model": os.environ.get("OBSCURA_EMBEDDING_MODEL", "unknown"),
            "embedding_version": os.environ.get("OBSCURA_EMBEDDING_VERSION", "unknown"),
            "embedding_ts": datetime.now(UTC).isoformat(),
        }

        if expires_at:
            payload["expires_at"] = expires_at.isoformat()
        try:
            self.client.upsert(
                self.collection_name,
                [PointStruct(id=point_id, vector=embedding, payload=payload)],
            )
        except Exception:
            logger.exception(
                "Failed to upsert vector for %s:%s", key.namespace, key.key
            )
            raise

    def purge_expired(self, batch_size: int = 10000) -> int:
        """Scan the collection for expired points and delete them in batches.

        This is best-effort: it pages through up to batch_size points per scroll
        invocation and deletes points whose payload.expires_at is in the past.
        Returns number of deleted points.
        """
        deleted = 0
        try:
            points, _ = self.client.scroll(
                self.collection_name,
                limit=batch_size,
                with_payload=True,
                with_vectors=False,
            )
            if not points:
                return 0
            to_delete = []
            for p in points:
                try:
                    if "expires_at" in p.payload and datetime.now(UTC) > datetime.fromisoformat(p.payload["expires_at"]):
                        to_delete.append(p.id)
                except Exception:
                    # Ignore malformed payloads
                    continue
            if to_delete:
                self.client.delete(self.collection_name, to_delete)
                deleted = len(to_delete)
        except Exception:
            logger.exception("Failed to purge expired vectors for %s", self.collection_name)
        return deleted

    def _start_gc_thread(self) -> None:
        """Start a background daemon thread that periodically purges expired points.

        Enabled via env var OBSCURA_QDRANT_ENABLE_GC=1. Interval in seconds via
        OBSCURA_QDRANT_GC_INTERVAL (default: 3600).
        """
        def _loop():
            interval = int(os.environ.get("OBSCURA_QDRANT_GC_INTERVAL", "3600"))
            while True:
                try:
                    deleted = self.purge_expired()
                    if deleted:
                        logger.info("qdrant: purged %d expired points from %s", deleted, self.collection_name)
                except Exception:
                    logger.exception("qdrant: background GC encountered an error")
                time.sleep(interval)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
    def get_vector(self, key: MemoryKey) -> VectorEntry | None:
        point_id = _point_id(key.namespace, key.key)
        try:
            points = self.client.retrieve(
                self.collection_name,
                [point_id],
                with_payload=True,
                with_vectors=True,
            )
            if not points:
                return None
            p = points[0]
            if "expires_at" in p.payload and datetime.now(UTC) > datetime.fromisoformat(
                p.payload["expires_at"],
            ):
                self.delete_vector(key)
                return None
            return VectorEntry(
                key=MemoryKey(namespace=p.payload["namespace"], key=p.payload["key"]),
                text=p.payload["text"],
                embedding=p.vector,
                metadata=p.payload.get("metadata", {}),
                memory_type=p.payload.get("memory_type", "general"),
                created_at=datetime.fromisoformat(p.payload["created_at"]),
            )
        except Exception:
            return None
    def search_vectors(
        self,
        query_embedding: list[float],
        namespace: str | None,
        top_k: int,
        threshold: float | None = None,
        filters: list[MetadataFilter] | None = None,
    ) -> list[VectorEntry]:
        """Search for similar vectors and apply optional time-based decay to scores.

        Decay is applied as an exponential half-life: final_score = raw_score * (0.5 ** (age_seconds / half_life_seconds)).
        Configure half-life via OBSCURA_MEMORY_DECAY_HALF_LIFE_SECONDS (default: 30 days).
        """
        must = (
            [FieldCondition(key="namespace", match=MatchValue(value=namespace))]
            if namespace
            else []
        )
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=query_embedding,
            query_filter=Filter(must=must) if must else None,
            limit=top_k,
            score_threshold=threshold,
            with_payload=True,
            with_vectors=False,
        )
        entries = []
        # Half-life in seconds for time decay. Default: 30 days.
        try:
            half_life = float(os.environ.get("OBSCURA_MEMORY_DECAY_HALF_LIFE_SECONDS", str(30 * 24 * 3600)))
        except Exception:
            half_life = 30 * 24 * 3600
        now = datetime.now(UTC)
        for hit in response.points:
            # Skip expired
            if "expires_at" in hit.payload and now > datetime.fromisoformat(hit.payload["expires_at"]):
                continue
            try:
                created = datetime.fromisoformat(hit.payload["created_at"])
            except Exception:
                created = now
            age_seconds = (now - created).total_seconds()
            decay = 1.0
            if half_life > 0:
                decay = 0.5 ** (age_seconds / half_life)
            final_score = hit.score * decay if hit.score is not None else 0.0
            entries.append(
                VectorEntry(
                    key=MemoryKey(
                        namespace=hit.payload["namespace"],
                        key=hit.payload["key"],
                    ),
                    text=hit.payload["text"],
                    embedding=[],
                    metadata=hit.payload.get("metadata", {}),
                    memory_type=hit.payload.get("memory_type", "general"),
                    created_at=datetime.fromisoformat(hit.payload["created_at"]),
                    score=hit.score or 0.0,
                    rerank_score=decay,
                    final_score=final_score,
                ),
            )
        return entries

    def delete_vector(self, key: MemoryKey) -> bool:
        point_id = _point_id(key.namespace, key.key)
        return (
            self.client.delete(self.collection_name, [point_id]).status == "completed"
        )

    def list_keys(self, namespace: str | None = None) -> list[MemoryKey]:
        filt = (
            Filter(
                must=[
                    FieldCondition(key="namespace", match=MatchValue(value=namespace)),
                ],
            )
            if namespace
            else None
        )
        points, _ = self.client.scroll(
            self.collection_name,
            scroll_filter=filt,
            limit=10000,
            with_payload=True,
            with_vectors=False,
        )
        return [
            MemoryKey(namespace=p.payload["namespace"], key=p.payload["key"])
            for p in points
        ]

    def clear_namespace(self, namespace: str) -> int:
        points, _ = self.client.scroll(
            self.collection_name,
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="namespace", match=MatchValue(value=namespace)),
                ],
            ),
            limit=10000,
            with_payload=False,
            with_vectors=False,
        )
        if not points:
            return 0
        self.client.delete(self.collection_name, [p.id for p in points])
        return len(points)

    def get_stats(self) -> dict[str, Any]:
        info = self.client.get_collection(self.collection_name)
        return {
            "backend": "qdrant",
            "total_vectors": info.points_count,
            "collection_name": self.collection_name,
            "embedding_dim": self.config.embedding_dim,
        }

    def close(self) -> None:
        pass
