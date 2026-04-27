"""Tests for obscura.core.sessions — SessionStore."""

from obscura.core.sessions import SessionStore
from obscura.core.types import Backend, SessionRef


class TestSessionStore:
    def test_add_and_get(self) -> None:
        store = SessionStore()
        ref = SessionRef(session_id="s1", backend=Backend.COPILOT)
        store.add(ref)
        assert store.get("s1") is ref

    def test_get_missing(self) -> None:
        store = SessionStore()
        assert store.get("missing") is None

    def test_remove(self) -> None:
        store = SessionStore()
        ref = SessionRef(session_id="s1", backend=Backend.COPILOT)
        store.add(ref)
        store.remove("s1")
        assert store.get("s1") is None

    def test_remove_missing(self) -> None:
        store = SessionStore()
        store.remove("missing")  # Should not raise

    def test_list_all(self) -> None:
        store = SessionStore()
        store.add(SessionRef(session_id="s1", backend=Backend.COPILOT))
        store.add(SessionRef(session_id="s2", backend=Backend.CLAUDE))
        assert len(store.list_all()) == 2

    def test_list_all_filter_by_backend(self) -> None:
        store = SessionStore()
        store.add(SessionRef(session_id="s1", backend=Backend.COPILOT))
        store.add(SessionRef(session_id="s2", backend=Backend.CLAUDE))
        store.add(SessionRef(session_id="s3", backend=Backend.COPILOT))
        copilot = store.list_all(backend=Backend.COPILOT)
        assert len(copilot) == 2
        claude = store.list_all(backend=Backend.CLAUDE)
        assert len(claude) == 1

    def test_len(self) -> None:
        store = SessionStore()
        assert len(store) == 0
        store.add(SessionRef(session_id="s1", backend=Backend.COPILOT))
        assert len(store) == 1

    def test_contains(self) -> None:
        store = SessionStore()
        store.add(SessionRef(session_id="s1", backend=Backend.COPILOT))
        assert "s1" in store
        assert "s2" not in store
