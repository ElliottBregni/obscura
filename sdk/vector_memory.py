"""
sdk/vector_memory — Semantic memory with vector search.

Extends the memory system with embeddings and similarity search.
Agents can store memories and retrieve semantically similar ones.

Usage::

    from sdk.vector_memory import VectorMemoryStore
    
    store = VectorMemoryStore.for_user(user)
    
    # Store with automatic embedding
    store.set("python_async", "Async/await is Python's way to handle concurrency...")
    
    # Semantic search
    results = store.search_similar(
        "how do I run multiple things at once?",
        top_k=3
    )
    # Returns memories about async/concurrency even if keywords don't match
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np

from sdk.auth.models import AuthenticatedUser
from sdk.memory import MemoryKey, MemoryStore


# Simple embedding function (in production, use OpenAI, sentence-transformers, etc.)
def simple_embedding(text: str, dim: int = 384) -> list[float]:
    """
    Create a simple hash-based embedding for demo purposes.
    
    In production, replace with:
    - OpenAI text-embedding-3-small
    - sentence-transformers/all-MiniLM-L6-v2
    - Custom embedding model
    """
    # Hash the text to get deterministic "embedding"
    hash_bytes = hashlib.sha256(text.encode()).digest()
    
    # Convert to float array
    floats = []
    for i in range(0, len(hash_bytes), 4):
        chunk = hash_bytes[i:i+4]
        val = int.from_bytes(chunk, 'little', signed=True)
        floats.append(val / 2**31)  # Normalize to [-1, 1]
    
    # Pad or truncate to desired dimension
    if len(floats) < dim:
        floats = floats * (dim // len(floats) + 1)
    
    return floats[:dim]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    a_arr = np.array(a)
    b_arr = np.array(b)
    
    norm_a = np.linalg.norm(a_arr)
    norm_b = np.linalg.norm(b_arr)
    
    if norm_a == 0 or norm_b == 0:
        return 0.0
    
    return float(np.dot(a_arr, b_arr) / (norm_a * norm_b))


@dataclass
class VectorMemoryEntry:
    """A memory entry with vector embedding."""
    key: MemoryKey
    text: str  # The raw text content
    embedding: list[float]
    metadata: dict[str, Any]
    created_at: datetime
    score: float = 0.0  # Similarity score (set during search)


class VectorMemoryStore:
    """
    Semantic memory store with vector search.
    
    Each user gets an isolated SQLite database with:
    - Text content
    - Vector embeddings
    - Metadata
    - Efficient similarity search
    """
    
    _instances: dict[str, VectorMemoryStore] = {}
    _lock = threading.Lock()
    
    def __init__(
        self,
        user: AuthenticatedUser,
        db_path: Path | None = None,
        embedding_fn: Callable[[str], list[float]] | None = None,
    ):
        self.user = user
        self.user_id = user.user_id
        self._db_id = hashlib.sha256(self.user_id.encode()).hexdigest()[:16]
        
        if db_path is None:
            db_path = Path.home() / ".obscura" / "vector_memory" / f"{self._db_id}.db"
        
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.embedding_fn = embedding_fn or simple_embedding
        self.embedding_dim = len(self.embedding_fn("test"))
        
        self._local = threading.local()
        self._init_db()
    
    @classmethod
    def for_user(
        cls,
        user: AuthenticatedUser,
        embedding_fn: Callable[[str], list[float]] | None = None,
    ) -> VectorMemoryStore:
        """Get or create a vector memory store for the given user."""
        with cls._lock:
            if user.user_id not in cls._instances:
                cls._instances[user.user_id] = cls(user, embedding_fn=embedding_fn)
            return cls._instances[user.user_id]
    
    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn
    
    def _init_db(self) -> None:
        """Initialize the database schema with vector support."""
        conn = self._get_conn()
        
        # Main table for vector memories
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS vector_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace TEXT NOT NULL,
                key TEXT NOT NULL,
                text TEXT NOT NULL,
                embedding BLOB NOT NULL,  -- JSON array of floats
                metadata TEXT,  -- JSON
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                UNIQUE(namespace, key)
            )
        """)
        
        # Indexes
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vec_memory_ns_key 
            ON vector_memory(namespace, key)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vec_memory_expires 
            ON vector_memory(expires_at)
        """)
        
        conn.commit()
    
    def set(
        self,
        key: str | MemoryKey,
        text: str,
        metadata: dict[str, Any] | None = None,
        namespace: str = "default",
        ttl: timedelta | None = None,
    ) -> None:
        """
        Store text with automatic embedding generation.
        
        Args:
            key: The memory key
            text: The text content to store and embed
            metadata: Additional JSON-serializable metadata
            namespace: Logical grouping
            ttl: Optional time-to-live
        """
        if isinstance(key, str):
            key = MemoryKey(namespace=namespace, key=key)
        
        # Generate embedding
        embedding = self.embedding_fn(text)
        
        expires_at = None
        if ttl:
            expires_at = datetime.now(UTC) + ttl
        
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO vector_memory 
                (namespace, key, text, embedding, metadata, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(namespace, key) DO UPDATE SET
                text = excluded.text,
                embedding = excluded.embedding,
                metadata = excluded.metadata,
                created_at = CURRENT_TIMESTAMP,
                expires_at = excluded.expires_at
            """,
            (
                key.namespace,
                key.key,
                text,
                json.dumps(embedding),
                json.dumps(metadata) if metadata else None,
                expires_at,
            )
        )
        conn.commit()
    
    def get(self, key: str | MemoryKey, namespace: str = "default") -> VectorMemoryEntry | None:
        """Retrieve a specific memory entry by key."""
        if isinstance(key, str):
            key = MemoryKey(namespace=namespace, key=key)
        
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT namespace, key, text, embedding, metadata, created_at, expires_at
            FROM vector_memory 
            WHERE namespace = ? AND key = ?
            """,
            (key.namespace, key.key)
        ).fetchone()
        
        if row is None:
            return None
        
        # Check expiration
        if row['expires_at']:
            expires = datetime.fromisoformat(row['expires_at'])
            if datetime.now(UTC) > expires:
                self.delete(key)
                return None
        
        return VectorMemoryEntry(
            key=MemoryKey(namespace=row['namespace'], key=row['key']),
            text=row['text'],
            embedding=json.loads(row['embedding']),
            metadata=json.loads(row['metadata']) if row['metadata'] else {},
            created_at=datetime.fromisoformat(row['created_at']),
        )
    
    def search_similar(
        self,
        query: str,
        namespace: str | None = None,
        top_k: int = 5,
        threshold: float = -1.0,
    ) -> list[VectorMemoryEntry]:
        """
        Search for semantically similar memories.
        
        Args:
            query: The search query text
            namespace: Filter by namespace (None = all)
            top_k: Number of results to return
            threshold: Minimum similarity score (0-1)
        
        Returns:
            List of memories sorted by similarity (highest first)
        """
        query_embedding = self.embedding_fn(query)
        
        conn = self._get_conn()
        
        # Get all candidates (in production, use vector index like FAISS, pgvector)
        if namespace:
            rows = conn.execute(
                "SELECT * FROM vector_memory WHERE namespace = ?",
                (namespace,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM vector_memory").fetchall()
        
        # Compute similarities
        results = []
        for row in rows:
            # Check expiration
            if row['expires_at']:
                expires = datetime.fromisoformat(row['expires_at'])
                if datetime.now(UTC) > expires:
                    continue
            
            embedding = json.loads(row['embedding'])
            score = cosine_similarity(query_embedding, embedding)
            
            if score >= threshold:
                entry = VectorMemoryEntry(
                    key=MemoryKey(namespace=row['namespace'], key=row['key']),
                    text=row['text'],
                    embedding=embedding,
                    metadata=json.loads(row['metadata']) if row['metadata'] else {},
                    created_at=datetime.fromisoformat(row['created_at']),
                    score=score,
                )
                results.append(entry)
        
        # Sort by similarity (descending) and return top_k
        results.sort(key=lambda x: x.score, reverse=True)
        return results[:top_k]
    
    def delete(self, key: str | MemoryKey, namespace: str = "default") -> bool:
        """Delete a memory entry."""
        if isinstance(key, str):
            key = MemoryKey(namespace=namespace, key=key)
        
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM vector_memory WHERE namespace = ? AND key = ?",
            (key.namespace, key.key)
        )
        conn.commit()
        return cursor.rowcount > 0
    
    def list_keys(self, namespace: str | None = None) -> list[MemoryKey]:
        """List all memory keys."""
        conn = self._get_conn()
        
        if namespace:
            rows = conn.execute(
                "SELECT namespace, key FROM vector_memory WHERE namespace = ?",
                (namespace,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT namespace, key FROM vector_memory").fetchall()
        
        return [MemoryKey(namespace=r['namespace'], key=r['key']) for r in rows]
    
    def clear_namespace(self, namespace: str) -> int:
        """Clear all memories in a namespace."""
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM vector_memory WHERE namespace = ?",
            (namespace,)
        )
        conn.commit()
        return cursor.rowcount
    
    def get_stats(self) -> dict[str, Any]:
        """Get vector memory statistics."""
        conn = self._get_conn()
        
        total = conn.execute("SELECT COUNT(*) as count FROM vector_memory").fetchone()['count']
        
        namespaces = conn.execute(
            "SELECT namespace, COUNT(*) as count FROM vector_memory GROUP BY namespace"
        ).fetchall()
        
        return {
            "total_memories": total,
            "embedding_dim": self.embedding_dim,
            "namespaces": {r['namespace']: r['count'] for r in namespaces},
            "db_path": str(self.db_path),
        }
    
    def close(self) -> None:
        """Close the database connection."""
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


# Integration with Agent class
class SemanticMemoryMixin:
    """Mixin to add semantic memory capabilities to agents."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._vector_memory: VectorMemoryStore | None = None
    
    @property
    def vector_memory(self) -> VectorMemoryStore:
        """Get the vector memory store for this agent."""
        if self._vector_memory is None:
            self._vector_memory = VectorMemoryStore.for_user(self.user)
        return self._vector_memory
    
    def remember(self, text: str, key: str | None = None, **metadata) -> None:
        """Store a memory with semantic embedding."""
        if key is None:
            key = f"memory_{datetime.now(UTC).timestamp()}"
        
        self.vector_memory.set(
            key,
            text,
            metadata={
                "agent_id": self.id,
                "agent_name": self.config.name,
                **metadata
            },
            namespace=f"{self.config.memory_namespace}:semantic"
        )
    
    def recall(self, query: str, top_k: int = 3) -> list[VectorMemoryEntry]:
        """Recall semantically similar memories."""
        return self.vector_memory.search_similar(
            query,
            namespace=f"{self.config.memory_namespace}:semantic",
            top_k=top_k
        )
