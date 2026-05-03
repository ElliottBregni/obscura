# Phase 5 — Migration / Backfill

> **Status:** ready to execute.
> **Owner:** Elliott Bregni (`bregnie34@gmail.com`)
> **Drafted:** 2026-04-26
> **Predecessor:** [`00-overview.md`](./00-overview.md) §"Phase 5 — Migration / backfill", [`phase-1-scaffold.md`](./phase-1-scaffold.md).
> **Successor:** Phase 6 (tests). Phase 5 ships the production backfill + lazy on-touch path; Phase 6 adds the test fixtures specifically for backfill idempotency.

This document is implementation-ready. Hand it to an engineer; they should not need to ask further questions to land the work. All file paths are absolute on first introduction, then relative.

Phases 1-4 land:

- An `obscura.lightrag_memory` package with `LightRAGAdapter`, `HybridVectorMemoryStore`, scoring helpers, and the `_lightrag_enabled()` feature flag (Phase 1).
- Synchronous post-write fan-out via `HybridVectorMemoryStore.set()` (Phase 2).
- `search_hybrid()` with re-applied decay + usage tracking (Phase 3).
- The two new graph-aware tools (`memory_graph_query`, `memory_graph_explain`) (Phase 4).

Phase 5 fills two gaps:

1. **The backlog.** Existing chunks already in Qdrant/SQLite are not in the graph. Without backfill, hybrid search is blind to history.
2. **Lifecycle drift.** When `MemoryConsolidator` deletes consolidated episodes, the graph keeps stale references to deleted chunks.

---

## 1. Goal & non-goals

### Goal

This phase produces:

1. **`VectorBackend.update_metadata`** — a new protocol method for atomic per-key partial-payload merges, plus implementations for the Qdrant and SQLite backends (and a sketch for Postgres so it doesn't bit-rot).
2. **Lazy on-touch indexing** — `vector_memory.touch()` (`obscura/vector_memory/vector_memory.py:551`) opportunistically schedules a graph ingest when a chunk is touched and lacks `lr_indexed_at` metadata. Hot chunks index themselves first, no operator action needed.
3. **`obscura/lightrag_memory/backfill.py`** — the batch-backfill engine. A `BackfillEngine` class with `estimate()` and `run()` methods, library-callable so future API endpoints / supervisor jobs can reuse it. The CLI is a thin wrapper.
4. **`obscura memory backfill-graph`** — the CLI surface. Supports dry-run, namespace/type filters, batch sizing, rate-limiting, max-chunks cap, resume, and a `--retry-failed` mode.
5. **Consolidator graph-cleanup hook** — `MemoryConsolidator.consolidate()` (`obscura/vector_memory/consolidator.py:130`) gains a per-deleted-episode `lr_adapter.delete_safe(doc_id)` call, preventing dangling graph entries.
6. **Cost telemetry** — both estimated $ before run and actual $ after run; gates >$1 runs behind `--confirm`.

### Non-goals (explicit)

- **No new tools.** `memory_graph_query` / `memory_graph_explain` ship in Phase 4. This phase touches only the migration surface.
- **No scoring or query-path changes.** `search_hybrid()` lands in Phase 3. We do not modify it here.
- **No UI for migration progress.** CLI-only with `tqdm`-style progress. A future web-ui dashboard for backfill is an explicit follow-up.
- **No automatic / scheduled backfill.** Backfill is heavy and LLM-priced; we do not wire it into `run_maintenance()` or the supervisor's heartbeat. Lazy on-touch is the only "automatic" track and it is rate-limited at 5 inserts/sec.
- **No multi-user fan-out.** `obscura memory backfill-graph` operates on the currently-authenticated user (the user-id resolved by `auth.middleware`). A `--user` flag exists for operator override but defaults to "current".

---

## 2. Acceptance criteria

Each item below must pass before this phase merges.

1. **Protocol completeness.** `VectorBackend.update_metadata(key, partial)` exists in `obscura/vector_memory/backends/base.py` and is implemented by `QdrantBackend`, `SQLiteBackend`, and `PostgreSQLVectorBackend`. `runtime_checkable` still passes for all three.
2. **Idempotency on update_metadata.** Calling `update_metadata(key, {"foo": 1})` twice in a row leaves the payload as `{"foo": 1, ...rest}` — no duplication, no error. Calling on a missing key is a no-op (does not raise).
3. **Per-field merge semantics.** `update_metadata(key, {"a": 1})` followed by `update_metadata(key, {"b": 2})` results in `metadata == {"a": 1, "b": 2, ...rest}`. Disjoint fields do not race.
4. **Dry-run is read-only.** `obscura memory backfill-graph --dry-run` reports total chunks to index + estimated LLM cost without making any LLM calls and without writing to Qdrant payloads or the NetworkX graph. Verified by asserting `LightRAGAdapter.insert_safe` is never called and no `update_metadata` writes happen.
5. **Max-chunks honored exactly.** `obscura memory backfill-graph --max-chunks 10 --rate-limit 100` indexes exactly 10 chunks and stamps `lr_indexed_at` on exactly those 10. Re-running with the same flags indexes 0 (already indexed).
6. **Lazy on-touch schedules a single ingest.** Touching a non-graphed chunk schedules a background graph insert. Touching the same chunk a second time before the first completes does not schedule a second insert. After the insert succeeds, subsequent touches do not schedule new inserts.
7. **Lazy on-touch rate-limited.** Bursting 100 touches on 100 distinct un-indexed chunks within 1 second results in at most 5 ingest calls reaching `LightRAGAdapter.insert_safe` in that second; the rest are silently dropped (will be picked up on later touches).
8. **Lazy on-touch backoff.** A chunk with `lr_index_attempts >= 3` is excluded from the lazy path. The user must run `obscura memory backfill-graph --retry-failed` to re-attempt.
9. **Consolidator cleans up the graph.** Running `MemoryConsolidator.consolidate()` over a session that produces a summary calls `lr_adapter.delete_safe(doc_id)` for every deleted episode. Asserted by mocking the adapter and counting calls.
10. **Cost gate.** Estimated cost ≤ $1: backfill runs without prompt. Estimated cost > $1 in interactive (TTY) mode: prompts `Estimated cost: $X.XX. Continue? [y/N]`. Estimated cost > $1 in non-TTY mode without `--confirm`: refuses with non-zero exit.
11. **Resumability.** Killing a running backfill mid-batch (Ctrl-C / SIGTERM) leaves all chunks indexed up to that point with `lr_indexed_at` set. Re-running the same command (with or without `--resume`) picks up exactly where it stopped — no double-indexing, no skipped chunks. Verified by `kill -9` mid-run integration test.
12. **Single-process safety.** Running two `obscura memory backfill-graph` invocations concurrently for the same user fails fast in the second with a clear "another backfill is in progress" message and a path to the lock file.

---

## 3. `VectorBackend.update_metadata` — protocol addition

### Protocol diff (`obscura/vector_memory/backends/base.py`)

Insert immediately after `touch_vector` (after line 95) so related per-key mutations cluster together:

```python
@runtime_checkable
class VectorBackend(Protocol):
    """Protocol defining the vector backend interface."""

    # ... existing methods ...

    def touch_vector(self, key: MemoryKey) -> None:
        """Update ``accessed_at`` to now.  No-op if key doesn't exist."""
        ...

    def update_metadata(
        self,
        key: MemoryKey,
        partial: dict[str, Any],
    ) -> None:
        """Merge ``partial`` into the existing payload metadata for ``key``.

        Semantics:
            * **Per-key atomic.** The merge is applied as a single transaction
              against the row/point. Two concurrent calls to disjoint fields
              are race-free; concurrent updates to the *same* field race —
              last writer wins.
            * **No-op on missing.** Calling on a non-existent key returns
              quietly. Callers should not rely on the return value to detect
              existence (use ``get_vector`` for that).
            * **Shallow merge.** Top-level keys in ``partial`` overwrite
              top-level keys in the existing metadata. Nested dicts are
              replaced wholesale, not merged recursively.
            * **No vector touch.** The embedding is not re-computed and
              ``updated_at`` is not bumped — this is a metadata-only path,
              distinct from ``store_vector`` upsert.

        Raises:
            Exceptions from the underlying client only on transport-level
            errors (e.g. Qdrant connection lost). Best-effort: callers may
            catch and log.

        """
        ...

    def list_by_type(self, ...): ...
    # ... rest unchanged ...
```

### Implementation — Qdrant (`obscura/vector_memory/backends/qdrant_backend.py`)

Qdrant ships `set_payload` which is a server-side merge. Insert after `touch_vector` (after line 413):

```python
def update_metadata(
    self,
    key: MemoryKey,
    partial: dict[str, Any],
) -> None:
    """Merge ``partial`` into the existing payload's ``metadata`` dict.

    Implementation note: Qdrant's ``set_payload`` operates on top-level
    payload fields. Our schema (see ``store_vector`` line 138) puts the
    user-supplied metadata dict under the ``metadata`` payload field. To
    merge into ``metadata`` rather than the top-level payload, we use
    ``set_payload`` with the ``key="metadata"`` selector, which is the
    documented Qdrant pattern for nested-field merge.

    Concurrency: Qdrant's payload updates are transactional per-point.
    Two concurrent ``update_metadata`` calls to disjoint fields of the
    same point are race-free. Same-field updates: last writer wins.
    """
    point_id = _point_id(key.namespace, key.key)
    if not partial:
        return
    try:
        # `key="metadata"` — merge into the nested `metadata` dict, not
        # into the top-level payload (which holds namespace/key/text/etc.).
        self.client.set_payload(
            collection_name=self.collection_name,
            payload=partial,
            points=[point_id],
            key="metadata",
        )
    except Exception:
        # Best-effort: callers in the backfill loop log + continue.
        # Don't suppress in store_vector (different path).
        logger.exception(
            "Failed to update_metadata for %s:%s",
            key.namespace,
            key.key,
        )
        raise
```

The `key="metadata"` selector requires `qdrant-client>=1.5.0`. We are on `>=1.17.0` per `pyproject.toml`, so this is safe.

### Implementation — SQLite (`obscura/vector_memory/backends/sqlite_backend.py`)

SQLite's `json_patch` (RFC 7396) is available in 3.38+ (2022). macOS ships 3.43+, modern Ubuntu/Debian 3.40+. The fallback path (Python-side merge) is a one-line safety net; we expect the fast path to fire on every supported platform. Insert after `touch_vector` (after line 335):

```python
def update_metadata(
    self,
    key: MemoryKey,
    partial: dict[str, Any],
) -> None:
    """Merge ``partial`` into the JSON metadata column for the given key.

    Uses ``json_patch`` (RFC 7396) when available (SQLite ≥ 3.38, ~2022),
    falling back to a SELECT + Python-side merge + UPDATE for older
    sqlite3 builds. The fast path holds on macOS (system sqlite ≥ 3.43)
    and modern Linux distros.

    Concurrency: wrapped in a transaction so the read-modify-write is
    atomic per row. Two writers to disjoint fields produce the union;
    same-field writers race — last commit wins.
    """
    if not partial:
        return
    conn = self._get_conn()
    patch_json = json.dumps(partial)
    try:
        # Fast path: server-side JSON merge.
        conn.execute(
            """
            UPDATE vector_memory
               SET metadata = json_patch(COALESCE(metadata, '{}'), ?)
             WHERE namespace = ? AND key = ?
            """,
            (patch_json, key.namespace, key.key),
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        # `no such function: json_patch` on older sqlite — fall back.
        if "json_patch" not in str(e):
            raise
        cur = conn.execute(
            "SELECT metadata FROM vector_memory WHERE namespace = ? AND key = ?",
            (key.namespace, key.key),
        )
        row = cur.fetchone()
        if row is None:
            return  # no-op on missing key, per protocol contract
        existing = json.loads(row["metadata"]) if row["metadata"] else {}
        merged = {**existing, **partial}  # shallow merge per protocol
        conn.execute(
            "UPDATE vector_memory SET metadata = ? WHERE namespace = ? AND key = ?",
            (json.dumps(merged), key.namespace, key.key),
        )
        conn.commit()
```

### Implementation sketch — Postgres (`obscura/vector_memory/backends/postgres_backend.py`)

The Postgres backend already uses `JSONB` for metadata (line 80). Add `update_metadata` symmetrical to the Qdrant/SQLite versions:

```python
def update_metadata(
    self,
    key: Any,
    partial: dict[str, Any],
) -> None:
    """Merge ``partial`` into ``metadata`` via Postgres ``||`` operator."""
    if not partial:
        return
    conn = self._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE vector_memory.entries
                   SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                 WHERE user_id = %s AND namespace = %s AND key = %s
                """,
                (json.dumps(partial), self._user_id, key.namespace, key.key),
            )
        conn.commit()
    finally:
        self._put_conn(conn)
```

Postgres `jsonb || jsonb` is shallow merge with right-side priority — exactly the protocol contract.

---

## 4. New metadata fields — registry

Phase 5 introduces several persistent payload fields. Document them in **one place** so future engineers know what's allowed under `metadata.*`. Consider adding this table to `obscura/lightrag_memory/__init__.py` as a module docstring constant.

| Field                    | Type             | Purpose                                                                  | Set by                                                       |
| ------------------------ | ---------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------ |
| `lr_indexed_at`          | ISO 8601 string  | When the chunk was successfully ingested into the graph.                 | `LightRAGAdapter.insert_safe` post-success (Phase 2 + Phase 5). |
| `lr_index_attempts`      | int              | Number of failed attempts (reset to 0 on success).                       | `LightRAGAdapter.insert_safe` and `BackfillEngine.run()`.    |
| `lr_index_skip_reason`   | str (≤200 chars) | Most recent skip / failure reason (e.g. `"below_min_length"`, exception class + message). | `HybridVectorMemoryStore.set` (skip path) and `BackfillEngine.run()` (failure path). |
| `lr_index_last_error_at` | ISO 8601 string  | When the last failure occurred. Useful for backoff display.              | `BackfillEngine.run()` failure handler.                      |
| `access_count`           | int              | Cumulative read count (usage signal for hybrid scoring).                 | `_touch_and_count_async` (Phase 3).                          |

### Backwards compat

Legacy entries written before Phase 5 lack all of these fields. Every consumer **must** treat absence as zero / never-indexed:

```python
# Right:
if not entry.metadata.get("lr_indexed_at"):
    schedule_index(entry)

# Wrong (KeyError on legacy entries):
if entry.metadata["lr_indexed_at"] is None:
    ...
```

The discovery filter in `BackfillEngine.estimate()` and the lazy-touch hook in `vector_memory.touch()` both follow this rule by default.

---

## 5. Lazy on-touch indexing

### Hook location

The cleanest place to opportunistically index is `VectorMemoryStore.touch()` (`obscura/vector_memory/vector_memory.py:551`). This is the canonical "this chunk got reused" signal — every consumer that wants to bump access freshness already calls it (the explicit `.touch()` API, `obscura/profile/store.py:118`, and Phase 3's `_touch_and_count_async`). Hooking here picks up all of them.

### Why not `search_*` directly?

Both work. `search_hybrid` already calls `_touch_and_count_async` (Phase 3), which calls `update_metadata` for `access_count`. Putting the LightRAG-schedule logic in `touch()` rather than duplicating it across `search_similar`, `search_reranked`, and `search_hybrid` is the single-write-site move. Phase 3's `_touch_and_count_async` becomes the only **producer** of `touch()` calls in the search path; `touch()` is the only **consumer** that schedules graph indexing.

### Code (in `HybridVectorMemoryStore`, `obscura/lightrag_memory/hybrid_store.py`)

Override `touch()` rather than modifying the base `VectorMemoryStore.touch`:

```python
class HybridVectorMemoryStore(VectorMemoryStore):
    # ... __init__ as in Phase 1 / Phase 2 ...

    def __init__(self, user, *, lightrag_adapter, **kw):
        super().__init__(user, **kw)
        self._lr = lightrag_adapter
        self._ingest_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix=f"lr-ingest-{user.user_id[:8]}",
        )
        # Lazy-touch rate limiter — token bucket. See section 5.2.
        self._lazy_bucket = _TokenBucket(
            rate_per_sec=float(os.environ.get("OBSCURA_LR_LAZY_RPS", "5.0")),
            capacity=10,
        )
        # In-flight set so we don't double-schedule the same key while one
        # ingest is already executing.
        self._lazy_inflight: set[str] = set()
        self._lazy_inflight_lock = threading.Lock()

    def touch(self, key: str | MemoryKey, namespace: str = "default") -> None:
        # Always do the parent-class accessed_at bump — backfill must not
        # change the existing decay-freshness behaviour.
        super().touch(key, namespace=namespace)

        if not self._lr.indexable_types:
            return

        if isinstance(key, str):
            key = MemoryKey(namespace=namespace, key=key)

        # Read the entry to decide whether to schedule. This is one extra
        # backend round-trip per touch — acceptable because touch() is
        # already not on the hottest path (only called from explicit .touch()
        # and Phase 3's _touch_and_count_async, which itself runs in a
        # background thread).
        entry = self.backend.get_vector(key)
        if entry is None:
            return
        if entry.memory_type not in self._lr.indexable_types:
            return
        if entry.metadata.get("lr_indexed_at"):
            return  # already indexed
        if entry.metadata.get("lr_index_attempts", 0) >= 3:
            # Stop auto-retrying. Force user to run explicit
            # `obscura memory backfill-graph --retry-failed`.
            return

        # Don't double-schedule: if we already submitted this key and the
        # executor hasn't finished yet, skip.
        composite = f"{key.namespace}:{key.key}"
        with self._lazy_inflight_lock:
            if composite in self._lazy_inflight:
                return
            if not self._lazy_bucket.try_acquire():
                # Burst exceeded. Silently drop — another touch on the
                # same chunk later will retry. The bucket replenishes at
                # OBSCURA_LR_LAZY_RPS tokens/sec.
                return
            self._lazy_inflight.add(composite)

        def _do_lazy_index() -> None:
            try:
                doc_id = composite
                self._lr.insert_safe(
                    doc_id=doc_id,
                    text=entry.text,
                    metadata={
                        **entry.metadata,
                        "memory_type": entry.memory_type,
                        "obscura_key": key.key,
                        "obscura_namespace": key.namespace,
                    },
                )
                self.backend.update_metadata(
                    key,
                    {
                        "lr_indexed_at": datetime.now(UTC).isoformat(),
                        "lr_index_attempts": 0,
                    },
                )
            except Exception as exc:
                _log.debug(
                    "lazy index failed for %s: %s",
                    composite,
                    exc,
                )
                prior = entry.metadata.get("lr_index_attempts", 0)
                self.backend.update_metadata(
                    key,
                    {
                        "lr_index_attempts": prior + 1,
                        "lr_index_skip_reason": str(exc)[:200],
                        "lr_index_last_error_at": datetime.now(UTC).isoformat(),
                    },
                )
            finally:
                with self._lazy_inflight_lock:
                    self._lazy_inflight.discard(composite)

        self._ingest_executor.submit(_do_lazy_index)
```

### Token bucket helper

Place at module top of `hybrid_store.py`. ~20 lines, no external deps:

```python
class _TokenBucket:
    """Simple token bucket for rate-limiting lazy ingests.

    Capacity=10, refill=rate_per_sec tokens/second. ``try_acquire()`` returns
    True if a token was available, False otherwise. Non-blocking — callers
    drop the work on False.
    """

    def __init__(self, rate_per_sec: float, capacity: int) -> None:
        self._rate = rate_per_sec
        self._capacity = capacity
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def try_acquire(self, n: int = 1) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False
```

### Why drop on bucket-empty rather than queue?

Queuing risks unbounded memory if the user blasts through millions of touches faster than LightRAG can index. Dropping is fine because **the same key will be touched again** on the next `search_*` that returns it — the lazy path is opportunistic by design. If a chunk is so cold it never gets touched again, the operator should run an explicit backfill.

### Disable knob

`OBSCURA_LR_LAZY=off` short-circuits the entire lazy path. Useful for batch-import scenarios where the operator wants the explicit backfill to run first without contention.

---

## 6. Backfill CLI — full design

### Command spec

```text
obscura memory backfill-graph
    [--user <id>]               # default: current authenticated user
    [--namespace <ns>]          # default: all namespaces
    [--memory-types <list>]     # comma-separated; default: indexable_types from config
    [--batch-size <int>]        # default: 50
    [--rate-limit <float>]      # default: 1.0 chunks/sec
    [--max-chunks <int>]        # default: unlimited
    [--dry-run]
    [--confirm]                 # required if estimated cost > $1.00 in non-TTY
    [--resume]                  # explicit semantics; behavior is naturally
                                # idempotent already
    [--retry-failed]            # target only chunks with lr_index_attempts > 0
    [--include-episodes]        # opt-in expansion of indexable_types to include
                                # `episode` (off by default, expensive)
    [--log-file <path>]         # default: ~/.obscura/logs/backfill_<ts>.log
    [--json]                    # machine-readable output to stdout
```

### Flow

#### Step 1 — Acquire single-process lock

```python
lock_path = _backfill_lock_path(user)  # ~/.obscura/lightrag/<user_hash>/.backfill.lock
fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise click.ClickException(
        f"Another backfill is in progress for this user.\n"
        f"Lock file: {lock_path}\n"
        f"If you're sure no backfill is running, delete the lock and retry."
    )
```

The lock file holds the PID + start ISO timestamp for forensic debugging. Released via `finally` after `run()` (or on process exit).

#### Step 2 — Discovery phase

```python
# Pseudo-code; real impl in BackfillEngine.estimate()
indexable = set(config.indexable_types)  # default: {"fact", "summary", "general"}
if args.include_episodes:
    indexable.add("episode")
if args.memory_types:
    indexable = indexable.intersection(set(args.memory_types.split(",")))

todo: list[VectorEntry] = []
by_type: dict[str, int] = {}
for key in store.backend.list_keys(namespace=args.namespace):
    entry = store.backend.get_vector(key)
    if entry is None:
        continue
    if entry.memory_type not in indexable:
        continue
    md = entry.metadata
    if md.get("lr_indexed_at"):
        continue  # already done
    if not args.retry_failed and md.get("lr_index_attempts", 0) >= 3:
        continue  # backed-off; user must --retry-failed
    if args.retry_failed and md.get("lr_index_attempts", 0) == 0:
        continue  # only failed chunks in this mode
    if len(entry.text) < MIN_LENGTH:
        # Persist the skip reason so it shows in stats next run.
        store.backend.update_metadata(
            key,
            {"lr_index_skip_reason": "below_min_length"},
        )
        continue
    todo.append(entry)
    by_type[entry.memory_type] = by_type.get(entry.memory_type, 0) + 1
    if args.max_chunks and len(todo) >= args.max_chunks:
        break
```

`MIN_LENGTH` defaults to 50 chars; configurable as `[vector_memory.lightrag.min_chunk_chars]` in `~/.obscura/config.toml`. Below that, LightRAG's entity extractor reliably hallucinates entities from filler text.

#### Step 3 — Estimation phase

```python
@dataclass
class BackfillEstimate:
    total_chunks: int
    by_memory_type: dict[str, int]
    estimated_llm_calls: int
    estimated_cost_usd: float
```

Print summary:

```text
Backfill plan
─────────────
  user:           a8c3f9d1...
  namespace:      (all)
  total chunks:   1247
    fact:         812
    summary:      398
    general:      37
  estimated LLM:  4988 calls (~3.99M tokens)
  estimated cost: $0.62 USD
  rate limit:     1.0 chunks/sec  (~21 minutes wall clock)
```

If `--dry-run`: print the summary, exit 0. No further work.

If estimated cost > `$1.00`:

- TTY: prompt `Estimated cost: $X.XX. Continue? [y/N]`. Default no.
- Non-TTY without `--confirm`: refuse with exit code 2. Print:
  ```
  Estimated cost: $X.XX exceeds non-TTY threshold ($1.00).
  Pass --confirm to proceed.
  ```
- Non-TTY with `--confirm`: proceed.

The threshold is configurable via `OBSCURA_LR_BACKFILL_COST_THRESHOLD_USD` (default: `1.00`).

#### Step 4 — Indexing phase

```python
report = BackfillReport(...)
for batch in chunked(todo, args.batch_size):
    for entry in batch:
        try:
            await rate_limiter.acquire()  # blocks to honour --rate-limit
            await asyncio.to_thread(
                store._lr.insert_safe,
                doc_id=f"{entry.key.namespace}:{entry.key.key}",
                text=entry.text,
                metadata={...},  # same shape as Phase 2
            )
            store.backend.update_metadata(
                entry.key,
                {
                    "lr_indexed_at": datetime.now(UTC).isoformat(),
                    "lr_index_attempts": 0,
                },
            )
            report.chunks_indexed += 1
            report.actual_llm_calls += LLM_CALLS_PER_CHUNK  # from telemetry
        except Exception as exc:
            store.backend.update_metadata(
                entry.key,
                {
                    "lr_index_attempts": entry.metadata.get("lr_index_attempts", 0) + 1,
                    "lr_index_skip_reason": str(exc)[:200],
                    "lr_index_last_error_at": datetime.now(UTC).isoformat(),
                },
            )
            report.chunks_failed += 1
            report.failed_keys.append((entry.key.namespace, entry.key.key, str(exc)[:200]))
        finally:
            on_progress(report) if on_progress else None
```

The rate limiter is an `asyncio.Semaphore`-backed leaky bucket sized for `--rate-limit chunks/sec`. Blocking `acquire()` is fine — backfill is intentionally slow.

#### Step 5 — Resume

`--resume` is mostly explicit semantics. The discovery filter already excludes already-indexed chunks (`lr_indexed_at` set). With or without `--resume`, re-running the command picks up where it left off. The flag exists to make intent obvious in scripts and help text.

#### Step 6 — Final report

```text
Backfill complete
─────────────────
  duration:       21m 18s
  indexed:        1247 / 1247
  skipped:        0 (already indexed)
  failed:         0
  actual LLM:     5012 calls
  actual cost:    $0.63 USD (estimate was $0.62)
  log:            ~/.obscura/logs/backfill_20260426T143012Z.log
```

If errors:

```text
Backfill complete (with errors)
───────────────────────────────
  ... same as above ...
  failed:         12

  failed types:
    fact (8 of 812)
    summary (4 of 398)

Re-run with `obscura memory backfill-graph --retry-failed` after investigating.
First failure: `summary:auth_session_2026-03-12` — RateLimitError: 429 too many requests
```

JSON output (`--json`) emits a single JSON object on stdout matching `BackfillReport.to_dict()`.

---

## 7. `obscura/lightrag_memory/backfill.py` — module structure

Module layout below. The CLI in `obscura/cli/memory_commands.py` is a thin click wrapper.

```python
"""obscura.lightrag_memory.backfill — Batch + lazy graph backfill.

Library-callable engine: the CLI is a thin wrapper; future API
endpoints / supervisor tasks can call ``BackfillEngine.run()`` directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser
    from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
    from obscura.memory import MemoryKey
    from obscura.vector_memory.backends.base import VectorEntry

_log = logging.getLogger(__name__)

# Cost-estimation constants — see section 8 for derivation.
AVG_TOKENS_PER_CHUNK = 800
LLM_CALLS_PER_CHUNK = 4
COST_PER_1K_INPUT_TOKENS = 0.000150  # gpt-4o-mini default
COST_PER_1K_OUTPUT_TOKENS = 0.000600
DEFAULT_OUTPUT_RATIO = 0.25  # output tokens ≈ 25% of input tokens

MIN_CHUNK_CHARS = 50


@dataclass(frozen=True)
class BackfillConfig:
    """User-facing knobs. Mirrors the CLI flags 1:1."""

    namespace: str | None = None
    memory_types: frozenset[str] | None = None
    batch_size: int = 50
    rate_limit: float = 1.0
    max_chunks: int | None = None
    dry_run: bool = False
    retry_failed: bool = False
    include_episodes: bool = False
    log_file: Path | None = None


@dataclass
class BackfillEstimate:
    """Output of the discovery + cost-estimation phase."""

    total_chunks: int = 0
    by_memory_type: dict[str, int] = field(default_factory=dict)
    estimated_llm_calls: int = 0
    estimated_cost_usd: float = 0.0
    estimated_duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BackfillReport:
    """Output of a completed (or partially-completed) run."""

    chunks_indexed: int = 0
    chunks_skipped: int = 0
    chunks_failed: int = 0
    duration_seconds: float = 0.0
    actual_llm_calls: int = 0
    actual_cost_usd: float = 0.0
    failed_keys: list[tuple[str, str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BackfillEngine:
    """Library-callable batch backfill.

    Usage::

        engine = BackfillEngine(store, config)
        estimate = engine.estimate()
        if not config.dry_run and confirm(estimate):
            report = engine.run(on_progress=tqdm_callback)
    """

    def __init__(
        self,
        store: HybridVectorMemoryStore,
        config: BackfillConfig,
    ) -> None:
        self.store = store
        self.config = config
        self._todo: list[VectorEntry] = []
        self._discovered = False

    # ------------------------------------------------------------------
    # Discovery + estimation
    # ------------------------------------------------------------------

    def estimate(self) -> BackfillEstimate:
        """Walk the backend, decide which chunks to index, and estimate cost."""
        self._discover()
        by_type: dict[str, int] = {}
        for entry in self._todo:
            by_type[entry.memory_type] = by_type.get(entry.memory_type, 0) + 1
        total = len(self._todo)
        llm_calls = total * LLM_CALLS_PER_CHUNK
        input_tokens = total * AVG_TOKENS_PER_CHUNK
        output_tokens = int(input_tokens * DEFAULT_OUTPUT_RATIO)
        cost = (
            input_tokens / 1000 * COST_PER_1K_INPUT_TOKENS
            + output_tokens / 1000 * COST_PER_1K_OUTPUT_TOKENS
        )
        duration = total / max(self.config.rate_limit, 0.01)
        return BackfillEstimate(
            total_chunks=total,
            by_memory_type=by_type,
            estimated_llm_calls=llm_calls,
            estimated_cost_usd=cost,
            estimated_duration_seconds=duration,
        )

    def _discover(self) -> None:
        if self._discovered:
            return
        store = self.store
        cfg = self.config

        # Resolve the indexable type set.
        indexable = set(cfg.memory_types) if cfg.memory_types else set(
            store._lr.indexable_types,
        )
        if cfg.include_episodes:
            indexable.add("episode")

        todo: list[VectorEntry] = []
        for key in store.backend.list_keys(namespace=cfg.namespace):
            if cfg.max_chunks and len(todo) >= cfg.max_chunks:
                break
            entry = store.backend.get_vector(key)
            if entry is None:
                continue
            if entry.memory_type not in indexable:
                continue
            md = entry.metadata
            if md.get("lr_indexed_at"):
                continue
            attempts = md.get("lr_index_attempts", 0)
            if cfg.retry_failed:
                if attempts == 0:
                    continue  # `--retry-failed` only revisits failures
            else:
                if attempts >= 3:
                    continue  # backed-off; user must --retry-failed
            if len(entry.text) < MIN_CHUNK_CHARS:
                # Stamp skip reason so it shows in subsequent stats.
                with contextlib.suppress(Exception):
                    store.backend.update_metadata(
                        key, {"lr_index_skip_reason": "below_min_length"},
                    )
                continue
            todo.append(entry)
        self._todo = todo
        self._discovered = True

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        on_progress: Callable[[BackfillReport], None] | None = None,
    ) -> BackfillReport:
        """Execute the backfill. Synchronous; safe to call from CLI.

        Internally manages an asyncio loop for the LightRAG inserts.
        """
        if self.config.dry_run:
            raise RuntimeError("BackfillEngine.run() called with dry_run=True")
        self._discover()

        report = BackfillReport()
        start = time.monotonic()
        try:
            asyncio.run(self._run_async(report, on_progress))
        finally:
            report.duration_seconds = time.monotonic() - start
        return report

    async def _run_async(
        self,
        report: BackfillReport,
        on_progress: Callable[[BackfillReport], None] | None,
    ) -> None:
        # Leaky-bucket rate limiter.
        interval = 1.0 / max(self.config.rate_limit, 0.01)
        last_call = 0.0

        for entry in self._todo:
            # Honor rate limit.
            now = time.monotonic()
            wait = max(0.0, interval - (now - last_call))
            if wait > 0:
                await asyncio.sleep(wait)
            last_call = time.monotonic()

            try:
                await asyncio.to_thread(
                    self.store._lr.insert_safe,
                    doc_id=f"{entry.key.namespace}:{entry.key.key}",
                    text=entry.text,
                    metadata={
                        **entry.metadata,
                        "memory_type": entry.memory_type,
                        "obscura_key": entry.key.key,
                        "obscura_namespace": entry.key.namespace,
                    },
                )
                self.store.backend.update_metadata(
                    entry.key,
                    {
                        "lr_indexed_at": datetime.now(UTC).isoformat(),
                        "lr_index_attempts": 0,
                    },
                )
                report.chunks_indexed += 1
                report.actual_llm_calls += LLM_CALLS_PER_CHUNK
            except Exception as exc:  # noqa: BLE001
                _log.exception(
                    "backfill: failed to index %s:%s",
                    entry.key.namespace,
                    entry.key.key,
                )
                prior = entry.metadata.get("lr_index_attempts", 0)
                with contextlib.suppress(Exception):
                    self.store.backend.update_metadata(
                        entry.key,
                        {
                            "lr_index_attempts": prior + 1,
                            "lr_index_skip_reason": str(exc)[:200],
                            "lr_index_last_error_at": datetime.now(UTC).isoformat(),
                        },
                    )
                report.chunks_failed += 1
                report.failed_keys.append(
                    (entry.key.namespace, entry.key.key, str(exc)[:200]),
                )
            finally:
                if on_progress:
                    on_progress(report)

        # Approximate actual cost from actual LLM calls.
        # LightRAG itself logs actual token counts; if the adapter exposes
        # them via `_lr.last_call_stats()`, prefer that. Otherwise estimate.
        report.actual_cost_usd = (
            report.actual_llm_calls
            * AVG_TOKENS_PER_CHUNK
            / LLM_CALLS_PER_CHUNK
            / 1000
            * (COST_PER_1K_INPUT_TOKENS + COST_PER_1K_OUTPUT_TOKENS * DEFAULT_OUTPUT_RATIO)
        )


def _backfill_lock_path(user: AuthenticatedUser) -> Path:
    user_hash = hashlib.sha256(user.user_id.encode()).hexdigest()[:16]
    base = Path.home() / ".obscura" / "lightrag" / user_hash
    base.mkdir(parents=True, exist_ok=True)
    return base / ".backfill.lock"
```

### CLI wrapper — `obscura/cli/memory_commands.py`

A new file (does not currently exist) for memory-related sub-commands. Other commands can later move here.

```python
"""obscura.cli.memory_commands — `obscura memory <subcommand>`."""

from __future__ import annotations

import json as _json
import sys

import click

from obscura.cli.render import console, print_error, print_info


@click.group(name="memory")
def memory_group() -> None:
    """Memory backfill, statistics, and maintenance."""


@memory_group.command("backfill-graph")
@click.option("--user", default=None, help="User ID override (default: current)")
@click.option("--namespace", default=None, help="Filter by namespace")
@click.option(
    "--memory-types",
    default=None,
    help="Comma-separated memory types (default: indexable types from config)",
)
@click.option("--batch-size", default=50, type=int)
@click.option("--rate-limit", default=1.0, type=float, help="Chunks per second")
@click.option("--max-chunks", default=None, type=int)
@click.option("--dry-run", is_flag=True, help="Estimate only; no LLM calls")
@click.option("--confirm", is_flag=True, help="Required for non-TTY runs > $1")
@click.option("--resume", is_flag=True, help="Resume an interrupted run")
@click.option(
    "--retry-failed",
    is_flag=True,
    help="Re-attempt chunks with lr_index_attempts > 0",
)
@click.option("--include-episodes", is_flag=True, help="Include episode-type chunks")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
def backfill_graph(
    user: str | None,
    namespace: str | None,
    memory_types: str | None,
    batch_size: int,
    rate_limit: float,
    max_chunks: int | None,
    dry_run: bool,
    confirm: bool,
    resume: bool,
    retry_failed: bool,
    include_episodes: bool,
    as_json: bool,
) -> None:
    """Index existing chunks into the LightRAG knowledge graph."""
    from obscura.auth.middleware import resolve_current_user
    from obscura.lightrag_memory import _lightrag_enabled
    from obscura.lightrag_memory.backfill import BackfillConfig, BackfillEngine
    from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
    from obscura.vector_memory import VectorMemoryStore

    if not _lightrag_enabled():
        raise click.ClickException(
            "LightRAG is disabled. Set OBSCURA_LIGHTRAG=on and install the "
            "extra: `uv sync --extra lightrag`.",
        )

    auth_user = resolve_current_user(user)  # raises ClickException on miss
    store = VectorMemoryStore.for_user(auth_user)
    if not isinstance(store, HybridVectorMemoryStore):
        raise click.ClickException(
            "VectorMemoryStore is not a HybridVectorMemoryStore. "
            "Did Phase 4 wiring land?",
        )

    config = BackfillConfig(
        namespace=namespace,
        memory_types=frozenset(memory_types.split(",")) if memory_types else None,
        batch_size=batch_size,
        rate_limit=rate_limit,
        max_chunks=max_chunks,
        dry_run=dry_run,
        retry_failed=retry_failed,
        include_episodes=include_episodes,
    )

    engine = BackfillEngine(store, config)
    estimate = engine.estimate()

    # Print plan.
    if as_json:
        click.echo(_json.dumps({"estimate": estimate.to_dict()}))
    else:
        _print_estimate(estimate, config)

    if dry_run:
        return

    # Cost gate.
    threshold = float(_env("OBSCURA_LR_BACKFILL_COST_THRESHOLD_USD", "1.00"))
    if estimate.estimated_cost_usd > threshold:
        if sys.stdin.isatty():
            click.confirm(
                f"Estimated cost: ${estimate.estimated_cost_usd:.2f}. Continue?",
                abort=True,
            )
        elif not confirm:
            raise click.ClickException(
                f"Estimated cost: ${estimate.estimated_cost_usd:.2f} exceeds "
                f"non-TTY threshold (${threshold:.2f}). Pass --confirm to proceed.",
            )

    # Run with single-process lock.
    from obscura.lightrag_memory.backfill import _backfill_lock_path
    import fcntl as _fcntl
    import os as _os

    lock_path = _backfill_lock_path(auth_user)
    fd = _os.open(lock_path, _os.O_CREAT | _os.O_RDWR)
    try:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except BlockingIOError:
            raise click.ClickException(
                f"Another backfill is in progress for this user.\n"
                f"Lock file: {lock_path}",
            )
        # Run.
        report = engine.run(on_progress=_make_progress_callback(estimate))
        if as_json:
            click.echo(_json.dumps({"report": report.to_dict()}))
        else:
            _print_report(report, estimate)
    finally:
        _os.close(fd)


# Helper functions: _print_estimate, _print_report, _make_progress_callback
# omitted for brevity — they are tqdm-style with rich.console fallbacks.
```

### Registration

In `obscura/cli/__init__.py`, just below the kairos registration (after line 2889):

```python
from obscura.cli.memory_commands import memory_group as _memory_group  # noqa: E402

main.add_command(_memory_group)
```

---

## 8. Cost estimation constants

The constants below are documented inline in `backfill.py` and configurable via `[vector_memory.lightrag.cost]` in `~/.obscura/config.toml`. Estimation is **wide-bound** — order of magnitude is the design goal, not penny-accuracy.

| Constant                       | Default    | Source / rationale                                                                |
| ------------------------------ | ---------- | --------------------------------------------------------------------------------- |
| `AVG_TOKENS_PER_CHUNK`         | `800`      | Empirical: typical Obscura chunk after `set_searchable` is ~3-4kB UTF-8 → ~800 tokens via cl100k. |
| `LLM_CALLS_PER_CHUNK`          | `4`        | LightRAG `ainsert` makes 1 entity-extraction call, 1 relation-extraction call, 1 description-generation call, ~1 dedup/community call. Source: LightRAG `kg/operate.py`. Rounded up. |
| `COST_PER_1K_INPUT_TOKENS`     | `0.000150` | `gpt-4o-mini` Apr 2026 list price.                                                |
| `COST_PER_1K_OUTPUT_TOKENS`    | `0.000600` | Ditto.                                                                            |
| `DEFAULT_OUTPUT_RATIO`         | `0.25`     | LightRAG outputs structured JSON ~25% the size of input prompts. Empirical.       |
| `MIN_CHUNK_CHARS`              | `50`       | Below this, entity extractor hallucinates. Tunable via config.                    |

### Config override

```toml
[vector_memory.lightrag.cost]
llm_input_per_1k    = 0.000150
llm_output_per_1k   = 0.000600
avg_tokens_per_chunk = 800
llm_calls_per_chunk  = 4
output_ratio         = 0.25
```

`BackfillEngine.estimate()` reads these at construction time. If the model changes (gpt-4.1, claude-haiku-4, etc.), update the toml — no code change needed.

### Why estimate up-front instead of just running?

A 10k-chunk backfill at gpt-4o-mini rates is ~$5-10. At gpt-4o (10× the price) it is ~$50-100. The user must see the bill before committing. Forcing them to babysit a long-running command and tail logs to check spend is the wrong UX.

---

## 9. Consolidator integration — preventing dangling graph entries

### Problem

`MemoryConsolidator.consolidate()` (`obscura/vector_memory/consolidator.py:140-145`) deletes consolidated episode entries directly via `self.store.backend.delete_vector(e.key)`. Without intervention, the LightRAG graph keeps stale references to those keys → `search_hybrid` hits return nodes whose underlying `VectorEntry` no longer exists, and `hydrate_to_vector_entry` (Phase 3) silently drops them. Drift accumulates over time.

### Hook

Right after the backend delete, call `self.store._lr.delete_safe(doc_id)` if `self.store` is a `HybridVectorMemoryStore`.

### Diff (`obscura/vector_memory/consolidator.py`)

Around line 140:

```python
            # Delete originals
            for e in entries:
                try:
                    self.store.backend.delete_vector(e.key)
                    deleted += 1

                    # Phase 5: keep the LightRAG graph in sync with the backend.
                    # If the store is a HybridVectorMemoryStore, also drop the
                    # corresponding doc from the graph. Best-effort — failures
                    # here must not abort consolidation.
                    self._maybe_delete_from_graph(e.key)

                except Exception:
                    _log.debug("Failed to delete episode %s", e.key, exc_info=True)
```

Add the helper method on `MemoryConsolidator`:

```python
    def _maybe_delete_from_graph(self, key: MemoryKey) -> None:
        """If the store has a LightRAG adapter, mirror the deletion to the graph.

        Best-effort: any exception is logged + swallowed so consolidation
        continues. The next backfill / sweep will reconcile.
        """
        try:
            from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
        except ImportError:
            return  # extra not installed — nothing to do
        if not isinstance(self.store, HybridVectorMemoryStore):
            return
        try:
            doc_id = f"{key.namespace}:{key.key}"
            self.store._lr.delete_safe(doc_id)
        except Exception:
            _log.debug(
                "Failed to delete graph entry for consolidated episode %s",
                key,
                exc_info=True,
            )
```

The summary chunk created by `consolidate()` (around line 130) is **automatically** indexed into the graph by Phase 2's `HybridVectorMemoryStore.set()` — the existing path handles it. No extra hook needed for the summary.

### Bulk-delete optimisation (optional)

LightRAG ships `adelete_by_doc_ids([...])`. For sessions consolidating 50+ episodes, batching the delete is meaningfully faster:

```python
    def _delete_batch_from_graph(self, keys: list[MemoryKey]) -> None:
        """Bulk-delete graph entries for a list of consolidated episodes."""
        if not keys:
            return
        try:
            from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
        except ImportError:
            return
        if not isinstance(self.store, HybridVectorMemoryStore):
            return
        doc_ids = [f"{k.namespace}:{k.key}" for k in keys]
        try:
            # adapter exposes delete_safe per-doc; batch via async API.
            asyncio.run(self.store._lr.adelete_by_doc_ids(doc_ids))
        except Exception:
            _log.debug("Bulk graph delete failed", exc_info=True)
```

Wire by accumulating successful per-episode deletes into a list and calling `_delete_batch_from_graph` after the for-loop. Defer this optimization until profiling shows consolidation is graph-bound — the per-episode loop is fine for sessions <20 episodes (the common case).

### Thread-safety note

`MemoryConsolidator` runs from `VectorMemoryStore.run_maintenance()` (`vector_memory.py:570`), typically called on startup or via `obscura/heartbeat/`. It is not async. The `_lr.delete_safe()` façade is sync (per Phase 1's adapter contract). Calling it from the consolidator's thread is safe.

If `for_user` is called concurrently from another thread *during* consolidation, the store reference held in `self.store` is the same singleton instance — there's no double-free risk. Any concurrent `set()` on the same key simply re-creates the chunk and (asynchronously) re-indexes it into the graph, which is the desired behaviour.

---

## 10. Dry-run safety

`obscura memory backfill-graph --dry-run` is the single most important affordance: operators must trust they can run it without burning money or mutating state.

### What dry-run does NOT do

- Does **not** call any LLMs. `LightRAGAdapter.insert_safe` is never invoked. (Asserted in tests by inspecting a mock adapter's call list.)
- Does **not** modify Qdrant payloads. `update_metadata` is never called in dry-run mode.
- Does **not** modify the NetworkX graph file (`~/.obscura/lightrag/<user_hash>/graph_chunk_entity_relation.gpickle`).
- Does **not** write to the consolidator path.
- Does **not** touch `accessed_at` (no `touch_vector` calls).
- Does **not** acquire the `.backfill.lock` file (the lock is for live runs only — concurrent dry-runs are fine).

### What dry-run DOES do

- Reads from Qdrant (cheap — `list_keys` + `get_vector` per key).
- Reads `~/.obscura/config.toml` for cost-estimation constants.
- Prints the structured plan to stdout.
- Exits 0 on success.

### Guarantees in the engine

`BackfillEngine.run(...)` raises `RuntimeError("dry_run=True")` if called with `BackfillConfig.dry_run=True`. That trips on misuse — the CLI is responsible for short-circuiting before calling `run()`.

### Output format

```text
Backfill plan (DRY RUN)
───────────────────────
  user:           a8c3f9d1...
  namespace:      (all)
  memory_types:   fact, summary, general
  total chunks:   1247
    fact:         812
    summary:      398
    general:      37
  estimated LLM:  4988 calls
  estimated cost: $0.62 USD
  rate limit:     1.0 chunks/sec
  estimated wall: 21m

NO CHANGES MADE. Re-run without --dry-run to execute.
```

With `--json`:

```json
{
  "estimate": {
    "total_chunks": 1247,
    "by_memory_type": {"fact": 812, "summary": 398, "general": 37},
    "estimated_llm_calls": 4988,
    "estimated_cost_usd": 0.62,
    "estimated_duration_seconds": 1247.0
  },
  "dry_run": true
}
```

The JSON form is grep/pipe-friendly for shell wrappers and CI dashboards.

---

## 11. Resumability and partial failure

### Failure modes

A backfill might be interrupted by:

1. **Ctrl-C** — clean signal; `KeyboardInterrupt` propagates from `asyncio.run`, the `finally` block releases the lock, the report is printed showing partial progress.
2. **OOM kill** — abrupt; lock file remains. The next run hits the lock and tells the user to delete it. (Future improvement: PID-aware locks that detect dead processes; out of scope for v1.)
3. **Network blip / 429 from LLM provider** — caught per-chunk; chunk is recorded as failed (`lr_index_attempts++`) and the loop continues.
4. **Adapter raises** (e.g. NetworkX pickle write fails) — same per-chunk handling; backfill keeps going.

### Resumability contract

Every chunk that successfully indexes gets `lr_indexed_at` written via `update_metadata` **before** the loop advances. There is no batching of metadata writes — each insert is paired with its own metadata update. This means:

- After Ctrl-C: chunks indexed up to that point have `lr_indexed_at` set; the next run skips them.
- After OOM: same, except the lock file blocks the next run until cleared.
- After per-chunk exception: `lr_index_attempts` is incremented and `lr_index_skip_reason` recorded; next run with default flags skips chunks with `attempts >= 3`.

### `--retry-failed`

Targets only chunks where `lr_index_attempts > 0`, regardless of count. Useful after fixing a transient cause (provider outage, expired API key). The flag bypasses the `>= 3` cap so a chunk that hit the cap can be retried.

```bash
# Step 1: see what failed
obscura memory backfill-graph --dry-run --retry-failed

# Step 2: retry
obscura memory backfill-graph --retry-failed
```

If a chunk fails again under `--retry-failed`, `lr_index_attempts` continues to increment past 3 (no cap on retry mode) and `lr_index_last_error_at` updates. The user can grep the log file for repeated failures and either fix the underlying issue or `delete_vector` the offending chunk.

### Failed-chunk forensics

When `BackfillReport.chunks_failed > 0`, the CLI prints:

- Count by memory type (so the user sees if one type is systematically failing).
- The first 3 failing keys + their truncated error.
- A pointer to the log file for full forensics.

Example output:

```text
Backfill complete (with errors)
  failed: 12 / 1247 (0.96%)
  failed types:
    fact (8 of 812)
    summary (4 of 398)
  first failures:
    fact:auth_2026-03-12 — RateLimitError: 429
    fact:auth_2026-03-13 — RateLimitError: 429
    fact:auth_2026-03-14 — TimeoutError
  log: ~/.obscura/logs/backfill_20260426T143012Z.log

Re-run with `obscura memory backfill-graph --retry-failed` after investigating.
```

---

## 12. Operational guardrails

### Single-process safety

Only one `BackfillEngine.run()` should execute at a time per user. Two concurrent backfills would:

- Double-count cost.
- Race on `update_metadata` (last-writer-wins still keeps payload consistent, but the count of chunks_indexed in each report would be wrong).
- In rare cases burn LLM tokens twice on the same chunk between when one process reads `lr_indexed_at == None` and the other writes it.

The lock file at `~/.obscura/lightrag/<user_hash>/.backfill.lock` is acquired with `fcntl.flock(LOCK_EX | LOCK_NB)`. If the lock is held, the second invocation prints:

```text
Error: Another backfill is in progress for this user.
Lock file: /Users/elliott/.obscura/lightrag/a8c3f9d1.../.backfill.lock
If you're sure no backfill is running, delete the lock and retry.
```

The lock is released on process exit (via the kernel) — so even a `kill -9` clears it, modulo the file existing. The "delete the lock and retry" hint covers the rare case where a stale lock survives.

`fcntl.flock` is POSIX. Windows is not supported in v1; if Windows support becomes important, use `msvcrt.locking()` behind a platform check. (Obscura is currently Mac/Linux per `pyproject.toml`.)

### Resource budget

- **LLM quota.** Default `--rate-limit 1.0` chunks/sec means ~4 LLM calls/sec at LLM_CALLS_PER_CHUNK=4. Most LLM providers (OpenAI tier-1, Anthropic, etc.) tolerate 50-500 RPM easily; this is well below all of them.
- **Network.** A LightRAG `ainsert` is ~2-4 LLM round-trips, each 100-500ms. At 1 chunk/sec, network is not the bottleneck.
- **CPU/RAM.** NetworkX graph file grows by a few KB per chunk indexed. 10k chunks → ~20-50MB file. Acceptable.
- **Foreground impact.** Backfill shares the LLM provider quota with interactive REPL turns. A user running a backfill in one terminal and chatting in another may see the chat throttled. Default `--rate-limit 1.0` is conservative for shared use; operators running off-hours can crank it to `5.0`.

### Logging

Every backfill writes a log file at `~/.obscura/logs/backfill_<YYYYMMDDTHHMMSSZ>.log` with one JSON-line per chunk:

```json
{"ts": "2026-04-26T14:30:13Z", "key": "fact:auth_2026-03-12", "outcome": "indexed", "duration_ms": 412, "attempt": 1}
{"ts": "2026-04-26T14:30:14Z", "key": "fact:auth_2026-03-13", "outcome": "failed", "error": "RateLimitError: 429", "attempt": 1}
```

The CLI summary at the end is a tail of this file. The `--log-file` flag overrides the default location — useful for piping into log aggregators.

### Configuration file precedent

All Phase 5 knobs live under `[vector_memory.lightrag]` in `~/.obscura/config.toml` (consistent with Phase 3's weights):

```toml
[vector_memory.lightrag]
indexable_types = ["fact", "summary", "general"]
min_chunk_chars = 50

[vector_memory.lightrag.lazy]
enabled    = true
rps        = 5.0
capacity   = 10
max_attempts_before_skip = 3

[vector_memory.lightrag.backfill]
default_rate_limit = 1.0
default_batch_size = 50
cost_threshold_usd = 1.00

[vector_memory.lightrag.cost]
llm_input_per_1k    = 0.000150
llm_output_per_1k   = 0.000600
avg_tokens_per_chunk = 800
llm_calls_per_chunk  = 4
output_ratio         = 0.25
```

Env vars override (case-insensitive): `OBSCURA_LR_LAZY`, `OBSCURA_LR_LAZY_RPS`, `OBSCURA_LR_BACKFILL_COST_THRESHOLD_USD`. Existing precedent in `obscura/core/config.py`.

---

## 13. Tests for this phase

Tests live at `tests/unit/obscura/lightrag_memory/test_backfill.py` and `tests/unit/obscura/vector_memory/test_update_metadata.py` (the latter covers the protocol addition independent of LightRAG).

### Backend `update_metadata` tests

Parametrized over `[QdrantBackend, SQLiteBackend, PostgreSQLVectorBackend]` using the in-memory Qdrant + tmp-path SQLite fixtures from `tests/unit/obscura/vector_memory/test_vector_memory.py:36-40`.

```python
@pytest.fixture(params=["sqlite", "qdrant_memory"])
def backend(request, tmp_path):
    if request.param == "sqlite":
        cfg = BackendConfig(user_id="u1", embedding_dim=4)
        return SQLiteBackend(cfg, db_path=tmp_path / "vm.db")
    if request.param == "qdrant_memory":
        cfg = BackendConfig(user_id="u1", embedding_dim=4)
        return QdrantBackend(cfg, mode="memory")
    raise AssertionError

def test_update_metadata_merges_disjoint_fields(backend):
    key = MemoryKey("ns", "k1")
    backend.store_vector(key, "txt", [0.1, 0.2, 0.3, 0.4],
                         {"a": 1}, "fact", None)
    backend.update_metadata(key, {"b": 2})
    entry = backend.get_vector(key)
    assert entry.metadata == {"a": 1, "b": 2}

def test_update_metadata_overwrites_same_field(backend):
    key = MemoryKey("ns", "k1")
    backend.store_vector(key, "txt", [0.1, 0.2, 0.3, 0.4],
                         {"a": 1}, "fact", None)
    backend.update_metadata(key, {"a": 99})
    entry = backend.get_vector(key)
    assert entry.metadata == {"a": 99}

def test_update_metadata_missing_key_is_noop(backend):
    backend.update_metadata(MemoryKey("ns", "missing"), {"a": 1})  # no raise

def test_update_metadata_empty_dict_is_noop(backend):
    key = MemoryKey("ns", "k1")
    backend.store_vector(key, "txt", [0.1, 0.2, 0.3, 0.4],
                         {"a": 1}, "fact", None)
    backend.update_metadata(key, {})
    entry = backend.get_vector(key)
    assert entry.metadata == {"a": 1}
```

### `BackfillEngine.estimate` — fixture corpus

```python
def test_estimate_counts_indexable_only(hybrid_store_with_corpus):
    # Fixture seeds 10 fact, 5 summary, 5 general, 3 episode.
    # indexable_types defaults to {fact, summary, general}.
    config = BackfillConfig()
    engine = BackfillEngine(hybrid_store_with_corpus, config)
    estimate = engine.estimate()
    assert estimate.total_chunks == 20  # excludes 3 episodes
    assert estimate.by_memory_type == {"fact": 10, "summary": 5, "general": 5}

def test_estimate_excludes_already_indexed(hybrid_store_with_corpus):
    # Mark 5 of the 10 facts as already indexed.
    keys = [k for k in hybrid_store_with_corpus.backend.list_keys() if k.namespace == "fact"][:5]
    for k in keys:
        hybrid_store_with_corpus.backend.update_metadata(k, {"lr_indexed_at": "2026-01-01T00:00:00Z"})
    config = BackfillConfig()
    engine = BackfillEngine(hybrid_store_with_corpus, config)
    estimate = engine.estimate()
    assert estimate.total_chunks == 15

def test_estimate_excludes_below_min_length(hybrid_store_with_short_chunks):
    # Fixture has 5 chunks with text < MIN_CHUNK_CHARS.
    config = BackfillConfig()
    engine = BackfillEngine(hybrid_store_with_short_chunks, config)
    estimate = engine.estimate()
    assert estimate.total_chunks == 0  # all rejected

def test_estimate_cost_in_dollars(hybrid_store_with_corpus):
    config = BackfillConfig()
    engine = BackfillEngine(hybrid_store_with_corpus, config)
    estimate = engine.estimate()
    # 20 chunks * 800 tokens * 4 calls — 64k input tokens.
    # At $0.000150 / 1k = $0.0096 input.
    # output ≈ 25% of input = 16k tokens * $0.000600 / 1k = $0.0096 output.
    # total ≈ $0.019 — wide bound check.
    assert 0.005 < estimate.estimated_cost_usd < 0.05
```

### `BackfillEngine.run` — mocked adapter

```python
def test_run_indexes_all_eligible(hybrid_store_with_corpus, mock_adapter):
    config = BackfillConfig(rate_limit=100)  # no rate-limiting in tests
    engine = BackfillEngine(hybrid_store_with_corpus, config)
    estimate = engine.estimate()
    report = engine.run()
    assert report.chunks_indexed == estimate.total_chunks
    assert report.chunks_failed == 0
    assert mock_adapter.insert_safe.call_count == estimate.total_chunks

def test_run_marks_indexed_at(hybrid_store_with_corpus, mock_adapter):
    config = BackfillConfig(rate_limit=100)
    engine = BackfillEngine(hybrid_store_with_corpus, config)
    engine.run()
    for key in hybrid_store_with_corpus.backend.list_keys():
        entry = hybrid_store_with_corpus.backend.get_vector(key)
        if entry.memory_type in {"fact", "summary", "general"}:
            assert entry.metadata.get("lr_indexed_at") is not None

def test_run_increments_attempts_on_failure(hybrid_store_with_corpus, failing_adapter):
    config = BackfillConfig(rate_limit=100)
    engine = BackfillEngine(hybrid_store_with_corpus, config)
    report = engine.run()
    assert report.chunks_failed == report.chunks_indexed + report.chunks_failed
    for key in hybrid_store_with_corpus.backend.list_keys():
        entry = hybrid_store_with_corpus.backend.get_vector(key)
        if entry.memory_type in {"fact", "summary", "general"}:
            assert entry.metadata.get("lr_index_attempts") == 1
            assert entry.metadata.get("lr_index_skip_reason") is not None
```

### Idempotency

```python
def test_run_twice_is_idempotent(hybrid_store_with_corpus, mock_adapter):
    config = BackfillConfig(rate_limit=100)
    engine = BackfillEngine(hybrid_store_with_corpus, config)
    report1 = engine.run()
    assert report1.chunks_indexed > 0

    engine2 = BackfillEngine(hybrid_store_with_corpus, config)
    report2 = engine2.run()
    assert report2.chunks_indexed == 0  # all already indexed
    assert mock_adapter.insert_safe.call_count == report1.chunks_indexed
```

### `--max-chunks`

```python
def test_run_honors_max_chunks(hybrid_store_with_corpus, mock_adapter):
    config = BackfillConfig(rate_limit=100, max_chunks=5)
    engine = BackfillEngine(hybrid_store_with_corpus, config)
    report = engine.run()
    assert report.chunks_indexed == 5
    assert mock_adapter.insert_safe.call_count == 5
```

### `--dry-run`

```python
def test_dry_run_makes_no_adapter_calls(hybrid_store_with_corpus, mock_adapter):
    config = BackfillConfig(dry_run=True)
    engine = BackfillEngine(hybrid_store_with_corpus, config)
    estimate = engine.estimate()
    assert estimate.total_chunks > 0
    # Don't call run() — CLI short-circuits. But verify direct invocation raises:
    with pytest.raises(RuntimeError, match="dry_run=True"):
        engine.run()
    assert mock_adapter.insert_safe.call_count == 0
```

### Lazy on-touch

```python
def test_touch_schedules_lazy_index_for_unindexed_chunk(
    hybrid_store, mock_adapter
):
    hybrid_store.set("k1", "some long text" * 20, memory_type="fact")
    # Phase 2 fan-out happens; reset the mock to simulate "post-Phase-2" state
    # where the chunk is in the backend but not yet in the graph.
    hybrid_store.backend.update_metadata(
        MemoryKey("default", "k1"), {"lr_indexed_at": None},  # explicitly clear
    )
    mock_adapter.insert_safe.reset_mock()

    hybrid_store.touch("k1")
    # Lazy path uses the executor — wait briefly.
    hybrid_store._ingest_executor.shutdown(wait=True)
    assert mock_adapter.insert_safe.call_count == 1

def test_touch_skips_already_indexed_chunk(hybrid_store, mock_adapter):
    hybrid_store.set("k1", "some long text" * 20, memory_type="fact")
    hybrid_store._ingest_executor.shutdown(wait=True)
    mock_adapter.insert_safe.reset_mock()

    hybrid_store.touch("k1")
    hybrid_store._ingest_executor.shutdown(wait=True)
    assert mock_adapter.insert_safe.call_count == 0  # already indexed

def test_lazy_rate_limit_drops_burst(hybrid_store, mock_adapter):
    # Seed 100 unindexed chunks.
    for i in range(100):
        hybrid_store.set(f"k{i}", "some long text" * 20, memory_type="fact")
        hybrid_store.backend.update_metadata(
            MemoryKey("default", f"k{i}"), {"lr_indexed_at": None},
        )
    mock_adapter.insert_safe.reset_mock()

    # Burst all 100 touches synchronously.
    for i in range(100):
        hybrid_store.touch(f"k{i}")

    hybrid_store._ingest_executor.shutdown(wait=True)
    # Bucket capacity 10 + ~5 refills/sec; should be much less than 100.
    assert mock_adapter.insert_safe.call_count <= 20

def test_lazy_skips_after_3_failures(hybrid_store, mock_adapter):
    hybrid_store.set("k1", "some long text" * 20, memory_type="fact")
    # Force attempts to 3.
    hybrid_store.backend.update_metadata(
        MemoryKey("default", "k1"),
        {"lr_indexed_at": None, "lr_index_attempts": 3},
    )
    mock_adapter.insert_safe.reset_mock()

    hybrid_store.touch("k1")
    hybrid_store._ingest_executor.shutdown(wait=True)
    assert mock_adapter.insert_safe.call_count == 0
```

### Consolidator graph cleanup

```python
def test_consolidate_deletes_graph_entries_for_episodes(
    hybrid_store_with_episodes, mock_adapter,
):
    # Fixture: 3 sessions, 4 episodes each = 12 episodes, all old enough.
    consolidator = MemoryConsolidator(
        store=hybrid_store_with_episodes,
        config=hybrid_store_with_episodes.decay_config,
    )
    deleted, summaries = consolidator.consolidate()
    assert deleted == 12
    assert summaries == 3
    # delete_safe called once per deleted episode.
    assert mock_adapter.delete_safe.call_count == 12
    # ... and 3 summary inserts (Phase 2 fan-out from `set()`).
    assert mock_adapter.insert_safe.call_count == 3

def test_consolidate_swallows_graph_errors(
    hybrid_store_with_episodes, failing_delete_adapter,
):
    consolidator = MemoryConsolidator(
        store=hybrid_store_with_episodes,
        config=hybrid_store_with_episodes.decay_config,
    )
    deleted, summaries = consolidator.consolidate()
    # Backend deletes still succeed; graph delete failures are swallowed.
    assert deleted == 12
    assert summaries == 3
```

### CLI integration

Use `click.testing.CliRunner` + the `MockLightRAG` fixture from Phase 6:

```python
def test_cli_dry_run_no_changes(monkeypatch, tmp_lr_user, mock_adapter):
    monkeypatch.setenv("OBSCURA_LIGHTRAG", "on")
    runner = CliRunner()
    result = runner.invoke(memory_group, ["backfill-graph", "--dry-run"])
    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert mock_adapter.insert_safe.call_count == 0

def test_cli_lock_file_blocks_concurrent_runs(tmp_lr_user, mock_adapter):
    # Acquire the lock manually, then invoke.
    lock_path = _backfill_lock_path(tmp_lr_user)
    with open(lock_path, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        runner = CliRunner()
        result = runner.invoke(memory_group, ["backfill-graph"])
        assert result.exit_code != 0
        assert "Another backfill is in progress" in result.output
```

### Don't test

- Real `lightrag-hku` insertions in unit tests. The mock adapter (`MockLightRAG` from Phase 6) is sufficient.
- LLM cost-estimation precision — checks should be wide-bound (order of magnitude).
- NetworkX graph file shape on disk — that's LightRAG's job to test.

---

## 14. Open questions / decisions deferred

### Should backfill ever index `episode` types?

**Default: no.** Episodes are turn-by-turn chatter that consolidates into summaries; indexing them in the graph means double-counting (the summary covers the same content). At ~$0.001/chunk and a typical 10k-episode user, that's ~$10 of unnecessary spend.

**Opt-in: `--include-episodes` flag.** Useful for:

- Users who want temporal graph reasoning ("what entities was I researching last Tuesday?").
- Pre-consolidation backfills.
- Eval / debugging.

The flag adds `episode` to the indexable set for that one invocation; it does not change `config.indexable_types`. To make episodes indexable globally, the user edits `~/.obscura/config.toml`.

### Server-side scheduler (cron-like nightly backfill)

Tempting: every night at 3am, scan for unindexed chunks and run a small incremental backfill so the graph stays warm.

**Out of scope for v1.** Reasons:

- Obscura currently runs locally per-user; "nightly" is unreliable on a laptop.
- LLM costs on autopilot is a category of bug we don't want.
- The lazy on-touch path covers 90% of the value (hot chunks index themselves) without any scheduling.

If/when this becomes important, it slots into `obscura/heartbeat/` as a tick handler. Implementation guidance: only run if (a) the user is idle for >15 min, (b) total cost-since-last-billing is below a configured ceiling, (c) the lock file is free. Do not log into a running interactive session.

### Backfill across all users

The CLI's `--user` flag accepts a single user id. For multi-user / shared deployments, an operator might want a cron sweep. Options:

- Iterate over all known users in `~/.obscura/users/` and invoke the engine per-user. Sequential to avoid quota contention.
- Add `obscura memory backfill-graph --all-users` as an explicit operator flag, behind a separate auth check.

Out of scope for v1; the local-first deployment model has one user per machine.

### Graph fragmentation after long-running deletes

Over months of consolidator runs, NetworkX's pickle file may accumulate orphan nodes (entities whose only mentions came from deleted episodes). LightRAG ships `aprune_orphans()` for this. A `obscura memory compact-graph` follow-up command could call it. Not blocking for v1; the prune is optional even at year-scale graph sizes.

### Storage of `failed_keys` in the report

The current `BackfillReport.failed_keys` is `list[tuple[str, str, str]]`. For a 10k-chunk run with many failures, this could be a large in-memory structure. If memory becomes a concern, switch to streaming the failures to the log file (which we already do) and returning only count + first-N samples in the report. Not a v1 concern.

---

## Effort summary

| Sub-deliverable                                             | Effort     | Notes |
| ----------------------------------------------------------- | ---------- | ----- |
| `VectorBackend.update_metadata` protocol + 3 backends       | 0.5 day    | Qdrant trivial; SQLite needs json_patch + fallback; Postgres trivial. |
| Lazy on-touch hook in `HybridVectorMemoryStore.touch`       | 0.5 day    | Plus `_TokenBucket` + in-flight set. |
| `BackfillEngine` library                                    | 0.75 day   | Discovery + estimate + run + lock file. |
| CLI wrapper + click registration                            | 0.5 day    | Includes `--json`, `--retry-failed`, progress callback. |
| Consolidator hook                                           | 0.25 day   | One method + the call site. |
| Unit tests (this phase + Phase 6 fixtures wired)            | 1 day      | ~12 tests; parametrised across 2 backends. |

**Net: ~3.5 days** with all phase-1-4 prereqs already landed.

---

## Definition of done

A reviewer should be able to verify each by running the listed command:

1. `pytest tests/unit/obscura/vector_memory/test_update_metadata.py -v` — all green.
2. `pytest tests/unit/obscura/lightrag_memory/test_backfill.py -v` — all green.
3. `OBSCURA_LIGHTRAG=on obscura memory backfill-graph --dry-run` — exits 0; prints plan; no LLM calls.
4. `OBSCURA_LIGHTRAG=on obscura memory backfill-graph --max-chunks 5 --rate-limit 100` — indexes exactly 5; report shows `chunks_indexed: 5`.
5. Re-run command 4 — `chunks_indexed: 0` (all eligible already indexed).
6. `OBSCURA_LIGHTRAG=on obscura memory backfill-graph --retry-failed` after a forced-failure run — picks only failed chunks.
7. Two concurrent invocations of command 4 — second one fails fast with the lock-file error.
8. `make lint` — clean. `make typecheck` — clean.
