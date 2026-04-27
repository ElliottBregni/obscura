"""Tests for obscura.admin.deletion.

These build miniature versions of each real store on disk so the walk
exercises actual DELETE statements and file unlinks rather than mocks.
That way a schema drift in one of the real stores breaks these tests
loudly — which is what we want for a SOC2-relevant control.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from obscura.admin import DeletionError, delete_user_data


USER = "alice@example.com"


# ---------------------------------------------------------------------------
# Mini-store builders
# ---------------------------------------------------------------------------


def _user_hash(user_id: str) -> str:
    return hashlib.sha256(user_id.encode()).hexdigest()[:16]


def _build_hashed_db(base: Path, user_id: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{_user_hash(user_id)}.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (k TEXT, v TEXT)")
        conn.execute("INSERT INTO t VALUES ('foo', 'bar')")
        conn.commit()
    finally:
        conn.close()
    # Simulate SQLite WAL sidecar files — the walk must clean them up too.
    (base / f"{_user_hash(user_id)}.db-wal").write_bytes(b"wal-stub")
    (base / f"{_user_hash(user_id)}.db-shm").write_bytes(b"shm-stub")
    return path


def _build_event_store(path: Path, user_id: str, other_user: str = "bob") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'running',
                active_agent TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                user_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE events (
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                PRIMARY KEY (session_id, seq)
            );
            """
        )
        conn.execute(
            "INSERT INTO sessions (id, created_at, updated_at, user_id) VALUES (?, ?, ?, ?)",
            ("s1", "2026-04-22T00:00:00Z", "2026-04-22T00:00:00Z", user_id),
        )
        conn.execute(
            "INSERT INTO sessions (id, created_at, updated_at, user_id) VALUES (?, ?, ?, ?)",
            ("s2", "2026-04-22T00:00:00Z", "2026-04-22T00:00:00Z", other_user),
        )
        conn.execute(
            "INSERT INTO sessions (id, created_at, updated_at, user_id) VALUES (?, ?, ?, ?)",
            ("orphan", "2026-04-22T00:00:00Z", "2026-04-22T00:00:00Z", ""),
        )
        for sid in ("s1", "s2", "orphan"):
            for n in range(3):
                conn.execute(
                    "INSERT INTO events (session_id, seq, kind, payload, timestamp) "
                    "VALUES (?, ?, 'k', '{}', '2026-04-22T00:00:00Z')",
                    (sid, n),
                )
        conn.commit()
    finally:
        conn.close()
    return path


def _build_notify(path: Path, user_id: str, other: str = "bob") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE messages (id TEXT PRIMARY KEY, user_id TEXT, body TEXT);
            CREATE TABLE dead_letters (id TEXT PRIMARY KEY, user_id TEXT, body TEXT);
            """
        )
        conn.execute(
            "INSERT INTO messages VALUES ('m1', ?, 'hello')", (user_id,)
        )
        conn.execute(
            "INSERT INTO messages VALUES ('m2', ?, 'hello')", (other,)
        )
        conn.execute(
            "INSERT INTO dead_letters VALUES ('d1', ?, 'oops')", (user_id,)
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _build_kairos(path: Path, user_id: str, other: str = "bob") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE kairos_goals (
                goal_id TEXT PRIMARY KEY,
                owner_id TEXT
            );
            CREATE TABLE kairos_plans (plan_id TEXT PRIMARY KEY, goal_id TEXT);
            CREATE TABLE kairos_tasks (task_id TEXT PRIMARY KEY, goal_id TEXT);
            CREATE TABLE kairos_checkpoints (cp_id TEXT PRIMARY KEY, goal_id TEXT);
            CREATE TABLE kairos_budget_usage (goal_id TEXT);
            """
        )
        conn.execute("INSERT INTO kairos_goals VALUES ('g-a', ?)", (user_id,))
        conn.execute("INSERT INTO kairos_goals VALUES ('g-b', ?)", (other,))
        conn.execute("INSERT INTO kairos_plans VALUES ('p-a', 'g-a')")
        conn.execute("INSERT INTO kairos_tasks VALUES ('t-a', 'g-a')")
        conn.execute("INSERT INTO kairos_checkpoints VALUES ('c-a', 'g-a')")
        conn.execute("INSERT INTO kairos_budget_usage VALUES ('g-a')")
        conn.execute("INSERT INTO kairos_plans VALUES ('p-b', 'g-b')")
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Set up all stores and override paths so we don't touch ~/.obscura/."""
    memory_dir = tmp_path / "memory"
    vector_dir = tmp_path / "vector"
    event_db = tmp_path / "supervisor.db"
    notify_db = tmp_path / "notify.db"
    kairos_db = tmp_path / "kairos.db"
    audit_log = tmp_path / "audit.jsonl"

    _build_hashed_db(memory_dir, USER)
    _build_hashed_db(vector_dir, USER)
    _build_event_store(event_db, USER)
    _build_notify(notify_db, USER)
    _build_kairos(kairos_db, USER)

    # Point audit at our tmp path so the tombstone lands somewhere we can read.
    monkeypatch.setenv("OBSCURA_AUDIT_LOG", str(audit_log))

    return {
        "memory_dir": memory_dir,
        "vector_dir": vector_dir,
        "event_db": event_db,
        "notify_db": notify_db,
        "kairos_db": kairos_db,
        "audit_log": audit_log,
    }


# ---------------------------------------------------------------------------
# Entry validation
# ---------------------------------------------------------------------------


def test_empty_user_id_rejected() -> None:
    with pytest.raises(DeletionError):
        delete_user_data("")
    with pytest.raises(DeletionError):
        delete_user_data("   ")


# ---------------------------------------------------------------------------
# Full walk — real stores, real writes
# ---------------------------------------------------------------------------


def test_walk_removes_user_data_across_all_stores(stores: dict[str, Path]) -> None:
    receipt = delete_user_data(
        USER,
        memory_dir=stores["memory_dir"],
        vector_memory_dir=stores["vector_dir"],
        event_store_path=stores["event_db"],
        notify_db_path=stores["notify_db"],
        kairos_db_path=stores["kairos_db"],
    )

    assert receipt.ok(), receipt.per_store
    assert receipt.dry_run is False

    # Memory + vector: files gone, WAL sidecars gone.
    user_hash = hashlib.sha256(USER.encode()).hexdigest()[:16]
    for base in ("memory_dir", "vector_dir"):
        d = stores[base]
        for suffix in ("", "-wal", "-shm", "-journal"):
            assert not (d / f"{user_hash}.db{suffix}").exists(), (
                f"{base}{suffix} not removed"
            )

    # Event store: user's sessions + events gone, other user's and orphan rows remain.
    conn = sqlite3.connect(str(stores["event_db"]))
    try:
        user_sessions = conn.execute(
            "SELECT id FROM sessions WHERE user_id = ?", (USER,)
        ).fetchall()
        assert user_sessions == []
        assert {row[0] for row in conn.execute("SELECT id FROM sessions")} == {
            "s2",
            "orphan",
        }
        # Events for s1 are gone; s2 and orphan's events remain.
        remaining = {
            row[0] for row in conn.execute("SELECT DISTINCT session_id FROM events")
        }
        assert remaining == {"s2", "orphan"}
    finally:
        conn.close()

    # Notify: user's rows gone; other's remains.
    conn = sqlite3.connect(str(stores["notify_db"]))
    try:
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE user_id = ?", (USER,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM dead_letters WHERE user_id = ?", (USER,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE user_id = 'bob'"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    # Kairos: user's goal gone and cascaded; other's remain.
    conn = sqlite3.connect(str(stores["kairos_db"]))
    try:
        assert conn.execute(
            "SELECT count(*) FROM kairos_goals WHERE owner_id = ?", (USER,)
        ).fetchone()[0] == 0
        for table in ("kairos_plans", "kairos_tasks", "kairos_checkpoints"):
            assert conn.execute(
                f"SELECT count(*) FROM {table} WHERE goal_id = 'g-a'"
            ).fetchone()[0] == 0
        # other user untouched
        assert conn.execute(
            "SELECT count(*) FROM kairos_goals WHERE owner_id = 'bob'"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    # Audit: tombstone appended with 'user.deletion' event
    lines = stores["audit_log"].read_text().strip().split("\n")
    tombstone = None
    for line in lines:
        record = json.loads(line)
        if record.get("event_type") == "user.deletion":
            tombstone = record
            break
    assert tombstone is not None, f"no tombstone in {lines}"
    assert tombstone["action"] == "delete"
    assert tombstone["outcome"] == "success"
    # PII never carried raw into the deletion record
    assert tombstone["user_email"] == "[REDACTED]"


def test_dry_run_reports_without_mutating(stores: dict[str, Path]) -> None:
    receipt = delete_user_data(
        USER,
        dry_run=True,
        memory_dir=stores["memory_dir"],
        vector_memory_dir=stores["vector_dir"],
        event_store_path=stores["event_db"],
        notify_db_path=stores["notify_db"],
        kairos_db_path=stores["kairos_db"],
    )

    assert receipt.dry_run is True
    assert receipt.ok()
    assert receipt.total_records() > 0
    for step in ("memory_kv", "vector_memory_sqlite", "event_store", "notify", "kairos"):
        assert receipt.per_store[step].get("dry_run") is True

    # Nothing deleted: user's db file + rows still there.
    user_hash = hashlib.sha256(USER.encode()).hexdigest()[:16]
    assert (stores["memory_dir"] / f"{user_hash}.db").exists()
    conn = sqlite3.connect(str(stores["event_db"]))
    try:
        assert conn.execute(
            "SELECT count(*) FROM sessions WHERE user_id = ?", (USER,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_orphan_sessions_without_user_id_are_preserved(stores: dict[str, Path]) -> None:
    """Pre-migration rows with user_id='' must not be swept up."""
    delete_user_data(
        USER,
        memory_dir=stores["memory_dir"],
        vector_memory_dir=stores["vector_dir"],
        event_store_path=stores["event_db"],
        notify_db_path=stores["notify_db"],
        kairos_db_path=stores["kairos_db"],
    )
    conn = sqlite3.connect(str(stores["event_db"]))
    try:
        assert conn.execute(
            "SELECT count(*) FROM sessions WHERE user_id = ''"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_absent_stores_return_clean_receipts(tmp_path: Path) -> None:
    """A missing DB file must report absent, not error."""
    receipt = delete_user_data(
        USER,
        memory_dir=tmp_path / "nowhere",
        vector_memory_dir=tmp_path / "also-nowhere",
        event_store_path=tmp_path / "nothing.db",
        notify_db_path=tmp_path / "still-nothing.db",
        kairos_db_path=tmp_path / "absent.db",
    )
    assert receipt.ok()
    for step in ("memory_kv", "vector_memory_sqlite", "event_store", "notify", "kairos"):
        assert receipt.per_store[step].get("note") == "absent"


def test_event_store_without_user_id_column_refuses(tmp_path: Path) -> None:
    """Before the migration ran, deletion must not silently succeed."""
    path = tmp_path / "pre-migration.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                status TEXT,
                active_agent TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE events (session_id TEXT, seq INTEGER, kind TEXT, payload TEXT, timestamp TEXT, PRIMARY KEY(session_id,seq));
            """
        )
        conn.commit()
    finally:
        conn.close()

    receipt = delete_user_data(
        USER,
        memory_dir=tmp_path / "m",
        vector_memory_dir=tmp_path / "v",
        event_store_path=path,
        notify_db_path=tmp_path / "n.db",
        kairos_db_path=tmp_path / "k.db",
    )
    # The overall walk returns a receipt — but event_store step records an error.
    assert not receipt.ok()
    assert "user_id" in receipt.per_store["event_store"]["error"]


def test_one_store_failing_does_not_stop_others(
    stores: dict[str, Path],
    tmp_path: Path,
) -> None:
    """Deletion is partial-success-tolerant: each step is isolated."""
    # Corrupt the notify DB to force a failure, keep the others usable.
    stores["notify_db"].write_bytes(b"not a database at all")

    receipt = delete_user_data(
        USER,
        memory_dir=stores["memory_dir"],
        vector_memory_dir=stores["vector_dir"],
        event_store_path=stores["event_db"],
        notify_db_path=stores["notify_db"],
        kairos_db_path=stores["kairos_db"],
    )

    assert not receipt.ok()
    assert "error" in receipt.per_store["notify"]
    # But the other steps still went through.
    user_hash = hashlib.sha256(USER.encode()).hexdigest()[:16]
    assert not (stores["memory_dir"] / f"{user_hash}.db").exists()
    conn = sqlite3.connect(str(stores["event_db"]))
    try:
        assert conn.execute(
            "SELECT count(*) FROM sessions WHERE user_id = ?", (USER,)
        ).fetchone()[0] == 0
    finally:
        conn.close()
