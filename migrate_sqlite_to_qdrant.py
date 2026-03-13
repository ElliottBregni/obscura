#!/usr/bin/env python3
"""Migrate vector memories from SQLite to Qdrant server at localhost:6333."""
import json, sqlite3
from datetime import timezone, datetime
from pathlib import Path
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

DB_HASH = "f944ab961e5ca0a9"
SQLITE_DB = Path.home() / ".obscura" / "vector_memory" / f"{DB_HASH}.db"
COLLECTION = f"user_{DB_HASH}"

print(f"Opening SQLite: {SQLITE_DB}")
con = sqlite3.connect(SQLITE_DB)
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT * FROM vector_memory WHERE expires_at IS NULL OR expires_at > ?",
    (datetime.now(timezone.utc).isoformat(),)
).fetchall()
con.close()
print(f"Found {len(rows)} non-expired rows")

client = QdrantClient(url="http://localhost:6333")
existing = [c.name for c in client.get_collections().collections]
print(f"Existing collections: {existing}")

if COLLECTION not in existing:
    first_emb = json.loads(rows[0]["embedding"])
    dim = len(first_emb)
    print(f"Creating collection '{COLLECTION}' with dim={dim}")
    client.create_collection(
        COLLECTION,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE)
    )
    client.create_payload_index(COLLECTION, "namespace", "keyword")
    client.create_payload_index(COLLECTION, "memory_type", "keyword")
else:
    print(f"Collection '{COLLECTION}' already exists")

points = []
skipped = 0
for row in rows:
    try:
        emb = json.loads(row["embedding"])
    except Exception as e:
        print(f"  Skipping row (bad embedding): {row['key']} -- {e}")
        skipped += 1
        continue
    pid = abs(hash(f"{row['namespace']}:{row['key']}")) % (2**63)
    payload = {
        "namespace": row["namespace"],
        "key": row["key"],
        "text": row["text"],
        "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
        "memory_type": row["memory_type"] or "general",
        "created_at": row["created_at"] or datetime.now(timezone.utc).isoformat(),
    }
    if row["expires_at"]:
        payload["expires_at"] = row["expires_at"]
    points.append(PointStruct(id=pid, vector=emb, payload=payload))

print(f"Built {len(points)} points ({skipped} skipped)")

BATCH = 100
for i in range(0, len(points), BATCH):
    batch = points[i:i+BATCH]
    client.upsert(collection_name=COLLECTION, points=batch)
    print(f"  Upserted {min(i+BATCH, len(points))}/{len(points)}")

info = client.get_collection(COLLECTION)
print(f"\nDone! Collection '{COLLECTION}' now has {info.points_count} vectors in Qdrant")
