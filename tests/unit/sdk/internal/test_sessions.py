"""Tests for sdk.internal.sessions — SessionStore and PersistentSessionStore."""

import json
import tempfile
from pathlib import Path

from sdk.internal.sessions import SessionStore, PersistentSessionStore
from sdk.internal.types import Backend, SessionRef


class TestSessionStore:
    def test_add_and_get(self):
        store = SessionStore()
        ref = SessionRef(session_id="s1", backend=Backend.COPILOT)
        store.add(ref)
        assert store.get("s1") is ref

    def test_get_missing(self):
        store = SessionStore()
        assert store.get("missing") is None

    def test_remove(self):
        store = SessionStore()
        ref = SessionRef(session_id="s1", backend=Backend.COPILOT)
        store.add(ref)
        store.remove("s1")
        assert store.get("s1") is None

    def test_remove_missing(self):
        store = SessionStore()
        store.remove("missing")  # Should not raise

    def test_list_all(self):
        store = SessionStore()
        store.add(SessionRef(session_id="s1", backend=Backend.COPILOT))
        store.add(SessionRef(session_id="s2", backend=Backend.CLAUDE))
        assert len(store.list_all()) == 2

    def test_list_all_filter_by_backend(self):
        store = SessionStore()
        store.add(SessionRef(session_id="s1", backend=Backend.COPILOT))
        store.add(SessionRef(session_id="s2", backend=Backend.CLAUDE))
        store.add(SessionRef(session_id="s3", backend=Backend.COPILOT))
        copilot = store.list_all(backend=Backend.COPILOT)
        assert len(copilot) == 2
        claude = store.list_all(backend=Backend.CLAUDE)
        assert len(claude) == 1

    def test_len(self):
        store = SessionStore()
        assert len(store) == 0
        store.add(SessionRef(session_id="s1", backend=Backend.COPILOT))
        assert len(store) == 1

    def test_contains(self):
        store = SessionStore()
        store.add(SessionRef(session_id="s1", backend=Backend.COPILOT))
        assert "s1" in store
        assert "s2" not in store


class TestPersistentSessionStore:
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.json"
            store = PersistentSessionStore(path)
            store.add(SessionRef(session_id="s1", backend=Backend.COPILOT))
            store.add(SessionRef(session_id="s2", backend=Backend.CLAUDE))
            store.save()

            assert path.exists()
            data = json.loads(path.read_text())
            assert len(data) == 2

            # Load into fresh store
            store2 = PersistentSessionStore(path)
            store2.load()
            assert len(store2) == 2
            assert store2.get("s1") is not None
            assert store2.get("s1").backend == Backend.COPILOT

    def test_load_no_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.json"
            store = PersistentSessionStore(path)
            store.load()  # Should not raise
            assert len(store) == 0

    def test_load_empty_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.json"
            path.write_text("")
            store = PersistentSessionStore(path)
            store.load()  # Should not raise
            assert len(store) == 0

    def test_save_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sub" / "dir" / "sessions.json"
            store = PersistentSessionStore(path)
            store.add(SessionRef(session_id="s1", backend=Backend.COPILOT))
            store.save()
            assert path.exists()


