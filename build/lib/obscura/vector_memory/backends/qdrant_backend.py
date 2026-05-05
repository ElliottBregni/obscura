"""Qdrant-based vector memory backend."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Condition,
    Distance,
    FieldCondition,
    Filter,
    KeywordIndexParams,
    MatchValue,
    PointStruct,
    VectorParams,
)

from obscura.memory import MemoryKey
from obscura.vector_memory.backends.base import BackendConfig, VectorEntry
from obscura.vector_memory.decay import compute_decay as _compute_decay

if TYPE_CHECKING:
    from obscura.vector_memory.vector_memory_filters import MetadataFilter


def _payload_dict(payload: Any) -> dict[str, Any]:
    """Coerce a qdrant Payload-or-None into a typed ``dict[str, Any]``.

    The qdrant client types ``point.payload`` as ``Payload | None`` where
    ``Payload = Dict[str, Any]``. Subscripting it without first checking for
    None trips pyright's strict-mode reportOptionalSubscript.  This helper
    centralises the None handling and gives downstream code a typed dict.
    """
    if payload is None:
        return {}
    return cast(dict[str, Any], payload)


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
        decay_config: Any = None,
        mode: str = "local",
        path: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.config = config
        self._decay_config = decay_config
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
            msg = f"Unknown Qdrant mode: {mode}"
            raise ValueError(msg)

        self._init_collection()

        # Enable background GC if opted in via env var
        if os.environ.get("OBSCURA_QDRANT_ENABLE_GC", "").strip() == "1":
            self._start_gc_thread()

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
                KeywordIndexParams(type=cast(Any, "keyword")),
            )
            self.client.create_payload_index(
                self.collection_name,
                "memory_type",
                KeywordIndexParams(type=cast(Any, "keyword")),
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
        now_iso = datetime.now(UTC).isoformat()

        # Preserve created_at on upsert — only set if this is a new point.
        existing_created_at: str = now_iso
        try:
            existing = self.client.retrieve(
                self.collection_name,
                [point_id],
                with_payload=True,
                with_vectors=False,
            )
            if existing:
                existing_payload = _payload_dict(existing[0].payload)
                if "created_at" in existing_payload:
                    existing_created_at = str(existing_payload["created_at"])
        except Exception:
            logger.debug("suppressed exception in store_vector", exc_info=True)

        payload: dict[str, Any] = {
            "namespace": key.namespace,
            "key": key.key,
            "text": text,
            "metadata": metadata,
            "memory_type": memory_type,
            "created_at": existing_created_at,
            "accessed_at": now_iso,
            # Embedding provenance (best-effort from env vars)
            "embedding_model": os.environ.get("OBSCURA_EMBEDDING_MODEL", "unknown"),
            "embedding_version": os.environ.get("OBSCURA_EMBEDDING_VERSION", "unknown"),
            "embedding_ts": now_iso,
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
                "Failed to upsert vector for %s:%s",
                key.namespace,
                key.key,
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
            to_delete: list[Any] = []
            for p in points:
                try:
                    payload = _payload_dict(p.payload)
                    if "expires_at" in payload and datetime.now(
                        UTC,
                    ) > datetime.fromisoformat(str(payload["expires_at"])):
                        to_delete.append(p.id)
                except Exception:
                    # Ignore malformed payloads
                    logger.debug("suppressed exception in purge_expired", exc_info=True)
                    continue
            if to_delete:
                self.client.delete(self.collection_name, to_delete)
                deleted = len(to_delete)
        except Exception:
            logger.exception(
                "Failed to purge expired vectors for %s",
                self.collection_name,
            )
        return deleted

    def _start_gc_thread(self) -> None:
        """Start a background daemon thread that periodically purges expired points.

        Enabled via env var OBSCURA_QDRANT_ENABLE_GC=1. Interval in seconds via
        OBSCURA_QDRANT_GC_INTERVAL (default: 3600).
        """

        def _loop() -> None:
            interval = int(os.environ.get("OBSCURA_QDRANT_GC_INTERVAL", "3600"))
            while True:
                try:
                    deleted = self.purge_expired()
                    if deleted:
                        logger.info(
                            "qdrant: purged %d expired points from %s",
                            deleted,
                            self.collection_name,
                        )
                except Exception:
                    logger.exception("qdrant: background GC encountered an error")
                time.sleep(interval)

        # daemon=True: intentional — GC is best-effort cleanup; losing an
        # in-flight purge cycle on interpreter shutdown is harmless.
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
            payload = _payload_dict(p.payload)
            if "expires_at" in payload and datetime.now(UTC) > datetime.fromisoformat(
                str(payload["expires_at"]),
            ):
                self.delete_vector(key)
                return None
            accessed_at_str = payload.get("accessed_at")
            embedding_raw = p.vector
            embedding: list[float] = (
                cast(list[float], embedding_raw) if embedding_raw is not None else []
            )
            metadata = cast(dict[str, Any], payload.get("metadata", {}))
            return VectorEntry(
                key=MemoryKey(
                    namespace=str(payload["namespace"]), key=str(payload["key"])
                ),
                text=str(payload["text"]),
                embedding=embedding,
                metadata=metadata,
                memory_type=str(payload.get("memory_type", "general")),
                created_at=datetime.fromisoformat(str(payload["created_at"])),
                accessed_at=datetime.fromisoformat(str(accessed_at_str))
                if accessed_at_str
                else None,
            )
        except Exception:
            logger.debug("suppressed exception in get_vector", exc_info=True)
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
        must: list[Condition] = (
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
        entries: list[VectorEntry] = []
        now = datetime.now(UTC)
        for hit in response.points:
            payload = _payload_dict(hit.payload)
            # Skip expired
            if "expires_at" in payload and now > datetime.fromisoformat(
                str(payload["expires_at"]),
            ):
                continue
            try:
                created = datetime.fromisoformat(str(payload["created_at"]))
            except Exception:
                logger.debug("suppressed exception in search_vectors", exc_info=True)
                created = now
            accessed_at_str = payload.get("accessed_at")
            accessed_at = (
                datetime.fromisoformat(str(accessed_at_str))
                if accessed_at_str
                else None
            )
            memory_type = str(payload.get("memory_type", "general"))

            # Per-type decay via centralized compute_decay
            decay: float
            if self._decay_config is not None:
                decay = float(
                    _compute_decay(
                        memory_type,
                        created,
                        accessed_at,
                        self._decay_config,
                        now=now,
                    )
                )
            else:
                # Legacy single half-life fallback
                half_life_raw: float | int | None = (
                    self.config.decay_half_life_seconds
                    if getattr(self.config, "decay_half_life_seconds", None) is not None
                    else 30 * 24 * 3600
                )
                half_life = float(half_life_raw) if half_life_raw is not None else 0.0
                age_seconds = (now - created).total_seconds()
                decay = 0.5 ** (age_seconds / half_life) if half_life > 0 else 1.0

            raw_score = hit.score or 0.0
            metadata = cast(dict[str, Any], payload.get("metadata", {}))
            entries.append(
                VectorEntry(
                    key=MemoryKey(
                        namespace=str(payload["namespace"]),
                        key=str(payload["key"]),
                    ),
                    text=str(payload["text"]),
                    embedding=[],
                    metadata=metadata,
                    memory_type=memory_type,
                    created_at=created,
                    accessed_at=accessed_at,
                    score=raw_score,
                    rerank_score=decay,
                    final_score=raw_score * decay,
                ),
            )
        return entries

    def delete_vector(self, key: MemoryKey) -> bool:
        point_id = _point_id(key.namespace, key.key)
        return (
            self.client.delete(self.collection_name, [point_id]).status == "completed"
        )

    def list_keys(self, namespace: str | None = None) -> list[MemoryKey]:
        ns_must: list[Condition] = (
            [FieldCondition(key="namespace", match=MatchValue(value=namespace))]
            if namespace
            else []
        )
        filt = Filter(must=ns_must) if ns_must else None
        points, _ = self.client.scroll(
            self.collection_name,
            scroll_filter=filt,
            limit=10000,
            with_payload=True,
            with_vectors=False,
        )
        keys: list[MemoryKey] = []
        for p in points:
            payload = _payload_dict(p.payload)
            keys.append(
                MemoryKey(namespace=str(payload["namespace"]), key=str(payload["key"]))
            )
        return keys

    def clear_namespace(self, namespace: str) -> int:
        ns_must: list[Condition] = [
            FieldCondition(key="namespace", match=MatchValue(value=namespace)),
        ]
        points, _ = self.client.scroll(
            self.collection_name,
            scroll_filter=Filter(must=ns_must),
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

    def touch_vector(self, key: MemoryKey) -> None:
        """Update ``accessed_at`` to now.  No-op if key doesn't exist."""
        point_id = _point_id(key.namespace, key.key)
        try:
            self.client.set_payload(
                self.collection_name,
                {"accessed_at": datetime.now(UTC).isoformat()},
                [point_id],
            )
        except Exception:
            logger.debug("suppressed exception in touch_vector", exc_info=True)

    def list_by_type(
        self,
        memory_type: str,
        older_than: datetime | None = None,
        limit: int = 100,
    ) -> list[VectorEntry]:
        """List entries of a given type, optionally filtered by age."""
        type_must: list[Condition] = [
            FieldCondition(key="memory_type", match=MatchValue(value=memory_type)),
        ]
        filt = Filter(must=type_must)
        points, _ = self.client.scroll(
            self.collection_name,
            scroll_filter=filt,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        entries: list[VectorEntry] = []
        for p in points:
            payload = _payload_dict(p.payload)
            try:
                created = datetime.fromisoformat(str(payload["created_at"]))
            except Exception:
                logger.debug("suppressed exception in list_by_type", exc_info=True)
                continue
            if older_than is not None and created >= older_than:
                continue
            accessed_at_str = payload.get("accessed_at")
            metadata = cast(dict[str, Any], payload.get("metadata", {}))
            entries.append(
                VectorEntry(
                    key=MemoryKey(
                        namespace=str(payload["namespace"]),
                        key=str(payload["key"]),
                    ),
                    text=str(payload["text"]),
                    embedding=[],
                    metadata=metadata,
                    memory_type=str(payload.get("memory_type", "general")),
                    created_at=created,
                    accessed_at=datetime.fromisoformat(str(accessed_at_str))
                    if accessed_at_str
                    else None,
                ),
            )
        return entries

    def close(self) -> None:
        pass
