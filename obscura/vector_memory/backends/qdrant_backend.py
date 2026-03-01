"""Qdrant-based vector memory backend."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from obscura.memory import MemoryKey
from obscura.vector_memory.backends.base import BackendConfig, VectorEntry
from obscura.vector_memory.vector_memory_filters import MetadataFilter


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
                "keyword",
            )
            self.client.create_payload_index(
                self.collection_name,
                "memory_type",
                "keyword",
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
        point_id = abs(hash(f"{key.namespace}:{key.key}")) % (2**63)
        payload = {
            "namespace": key.namespace,
            "key": key.key,
            "text": text,
            "metadata": metadata,
            "memory_type": memory_type,
            "created_at": datetime.now(UTC).isoformat(),
        }
        if expires_at:
            payload["expires_at"] = expires_at.isoformat()
        self.client.upsert(
            self.collection_name,
            [PointStruct(id=point_id, vector=embedding, payload=payload)],
        )

    def get_vector(self, key: MemoryKey) -> VectorEntry | None:
        point_id = abs(hash(f"{key.namespace}:{key.key}")) % (2**63)
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
        threshold: float | None,
        filters: list[MetadataFilter] | None,
    ) -> list[VectorEntry]:
        must = (
            [FieldCondition(key="namespace", match=MatchValue(value=namespace))]
            if namespace
            else []
        )
        results = self.client.search(
            self.collection_name,
            query_embedding,
            query_filter=Filter(must=must) if must else None,
            limit=top_k,
            score_threshold=threshold,
        )
        entries = []
        for hit in results:
            if "expires_at" in hit.payload and datetime.now(
                UTC,
            ) > datetime.fromisoformat(hit.payload["expires_at"]):
                continue
            entries.append(
                VectorEntry(
                    key=MemoryKey(
                        namespace=hit.payload["namespace"],
                        key=hit.payload["key"],
                    ),
                    text=hit.payload["text"],
                    embedding=hit.vector if hasattr(hit, "vector") else [],
                    metadata=hit.payload.get("metadata", {}),
                    memory_type=hit.payload.get("memory_type", "general"),
                    created_at=datetime.fromisoformat(hit.payload["created_at"]),
                    score=hit.score,
                ),
            )
        return entries

    def delete_vector(self, key: MemoryKey) -> bool:
        point_id = abs(hash(f"{key.namespace}:{key.key}")) % (2**63)
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
