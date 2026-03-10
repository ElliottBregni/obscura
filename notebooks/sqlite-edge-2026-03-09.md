# SQLite at the Edge: 2026 Overview

Date: 2026-03-09

This brief explains why SQLite has resurged for edge/serverless apps and how platforms implement it, with links to primary docs.

## Why SQLite fits the edge
- Zero-configuration, single-file DB lowers cold-start and ops overhead.
- Works well with ephemeral compute; data can be replicated to the edge or used read-mostly.
- Mature, battle-tested engine with excellent reliability and SQL features.

## Key platforms and approaches
- Cloudflare D1: Managed SQLite with Durable Objects for coordination; integrates with Workers. Docs cover schema, migrations, and limits.
- Fly.io LiteFS: FUSE-based replication that treats SQLite as primary, replicating pages across instances; supports reads from replicas.
- Turso: Managed “SQLite at the edge” with global replicas near users; offers libSQL protocol compatibility.

## Design considerations
- Consistency vs. latency: Some platforms favor read-local replicas with async replication; global write latency can be high if strict sync is required.
- Write patterns: Coalesce writes, avoid hot-row contention; prefer append-only logging where possible.
- Migrations: Use online migration strategies; test with production-like replicas first.
- Observability: Use page-cache hit metrics, WAL size, and replication lag to validate behavior.

## Quick start links (primary sources)
- Cloudflare D1 docs: https://developers.cloudflare.com/d1/
- Fly.io LiteFS docs: https://fly.io/docs/litefs/
- SQLite “serverless” page: https://sqlite.org/serverless.html

## Minimal schema example (WAL enabled)
```sql
PRAGMA journal_mode=WAL;
CREATE TABLE notes (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  body TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX notes_created_at ON notes(created_at);
```

## Operational tips
- Keep WAL size bounded; checkpoint on deploys.
- Use read replicas for heavy queries; pin writes to a coordinator region.
- Back up `.sqlite` files frequently; verify restore drills.

