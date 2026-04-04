import json
import os
import time
from pathlib import Path
from typing import Never

from obscura.kairos.dream import DreamConsolidator


def test_acquire_writes_json_and_is_locked(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    d = DreamConsolidator()

    assert d._acquire_lock() is True
    lock_path = d._lock_file()
    assert lock_path.exists()
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data.get("pid") == os.getpid()
    assert isinstance(data.get("ts"), float)
    assert d._is_locked() is True

    d._rollback_lock()


def test_stale_lock_allows_acquire(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    mem = Path(tmp_path) / ".obscura" / "memory"
    mem.mkdir(parents=True)
    lock = mem / ".consolidate-lock"
    old_meta = {"pid": 999999, "ts": time.time() - 10_000}
    lock.write_text(json.dumps(old_meta), encoding="utf-8")

    d = DreamConsolidator()

    # Stale PID should be considered not-locked
    assert d._is_locked() is False
    assert d._acquire_lock() is True
    data = json.loads(d._lock_file().read_text(encoding="utf-8"))
    assert data.get("pid") == os.getpid()

    d._rollback_lock()


def test_permission_error_treated_as_locked(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    mem = Path(tmp_path) / ".obscura" / "memory"
    mem.mkdir(parents=True)
    lock = mem / ".consolidate-lock"
    old_meta = {"pid": 12345, "ts": time.time()}
    lock.write_text(json.dumps(old_meta), encoding="utf-8")

    d = DreamConsolidator()

    # Patch os.kill to raise PermissionError to simulate restricted PID check.
    def fake_kill(pid, sig) -> Never:
        raise PermissionError

    monkeypatch.setattr(os, "kill", fake_kill)

    assert d._is_locked() is True
    assert d._acquire_lock() is False

    lock.unlink(missing_ok=True)
