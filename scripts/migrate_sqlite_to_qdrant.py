#!/usr/bin/env python3
"""Migrate vector memory from SQLite backend to Qdrant server (localhost:6333).

Usage:
    python3 scripts/migrate_sqlite_to_qdrant.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
DB_HASH = "f944ab961e5ca0a9"
SQLITE_DB = Path.home() / ".obscura" / "vector_memory" / f"{DB_HASH}.db"
COLLECTION = f"user_{DB_HASH}"
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
# ─────────────────────────────────────────────────────────────────────────────


def point_id(namespace: str, key: str) -> int:
    return abs(hash(f"{namespace}:{key}")) % (2**63)


def migrate(dry_run: bool = False) -> None:
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, PointStruct, VectorParams
    except ImportError:
        print("❌ qdrant-client not installed. Run: pip install qdrant-client")
        sys.exit(1)

    if not SQLITE_DB.exists():
        print(f"❌ SQLite DB not found: {SQLITE_DB}")
        sys.exit(1)

    # Connect to source SQLite
    con = sqlite3.connect(SQLITE_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM vector_memory WHERE expires_at IS NULL OR expires_at > ?",
        (datetime.now(UTC).isoformat(),),
    ).fetchall()
    con.close()

    print(f"📦 Found {len(rows)} non-expired vectors in SQLite")
    if not rows:
        print("✅ Nothing to migrate.")
        return

    # Connect to Qdrant
    client = QdrantClient(url=QDRANT_URL)

    # Ensure collection exists
    existing = [c.name for c in client.get_collections().collections]
    if COLLECTION not in existing:
        # Detect embedding dim from first row
        sample_emb = json.loads(rows[0]["embedding"])
        dim = len(sample_emb)
        print(f"   Creating collection '{COLLECTION}' (dim={dim})")
        if not dry_run:
            client.create_collection(
                collection_name=COLLECTION,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
            client.create_payload_index(COLLECTION, "namespace", "keyword")
            client.create_payload_index(COLLECTION, "memory_type", "keyword")
    else:
        print(f"   Collection '{COLLECTION}' already exists")

    # Build points
    points = []
    skipped = 0
    for row in rows:
        try:
            embedding = json.loads(row["embedding"])
        except (json.JSONDecodeError, TypeError):
            skipped += 1
            continue

        pid = point_id(row["namespace"], row["key"])
        payload = {
            "namespace": row["namespace"],
            "key": row["key"],
            "text": row["text"],
            "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
            "memory_type": row["memory_type"] or "general",
            "created_at": row["created_at"] or datetime.now(UTC).isoformat(),
        }
        if row["expires_at"]:
            payload["expires_at"] = row["expires_at"]

        points.append(PointStruct(id=pid, vector=embedding, payload=payload))

    print(f"   Migrating {len(points)} points (skipped {skipped} malformed)")

    if dry_run:
        print("🔍 DRY RUN — no data written to Qdrant")
        for p in points[:3]:
            print(
                f"   Sample: id={p.id} ns={p.payload['namespace']} key={p.payload['key'][:40]}"
            )
        return

    # Batch upsert in chunks of 100
    batch_size = 100
    for i in range(0, len(points), batch_size):
        batch = points[i : i + batch_size]
        client.upsert(collection_name=COLLECTION, points=batch)
        print(f"   Upserted {min(i + batch_size, len(points))}/{len(points)}")

    # Verify
    info = client.get_collection(COLLECTION)
    print(f"\n✅ Migration complete!")
    print(f"   Qdrant collection '{COLLECTION}': {info.points_count} total vectors")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate SQLite vector memory to Qdrant"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without writing"
    )
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
