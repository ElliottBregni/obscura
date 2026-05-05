"""Tests for the keyword-memory repository (SQLite backend + factory)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from obscura.data.keyword_memory import (
    KeywordMemoryRepo,
    Memory,
    get_keyword_memory_repo,
    keyword_memory_available,
)
from obscura.data.keyword_memory.sqlite import SqliteKeywordMemoryRepo


@pytest.fixture
def tmp_obscura_home() -> Path:
    """Force the data-layer engine to use a per-test SQLite directory."""
    td = tempfile.TemporaryDirectory()
    # Reset the schema-init flag so each test reseeds.
    SqliteKeywordMemoryRepo._schema_initialized = False  # pyright: ignore[reportPrivateUsage]
    with patch.dict(
        os.environ,
        {"OBSCURA_DB_URL": f"sqlite://{td.name}"},
        clear=False,
    ) as _env:
        for key in ("OBSCURA_PG_HOST", "OBSCURA_PG_PASSWORD"):
            _env.pop(key, None)
        yield Path(td.name)
    td.cleanup()


@pytest.mark.unit
class TestSqliteKeywordMemoryRepo:
    def test_round_trip(self, tmp_obscura_home: Path) -> None:
        repo = get_keyword_memory_repo()
        new_id = repo.remember("user prefers terse responses", namespace="user:prefs")
        assert new_id > 0

        results = repo.recall("terse")
        assert len(results) == 1
        assert results[0].id == new_id
        assert results[0].namespace == "user:prefs"
        assert "terse" in results[0].content
        assert results[0].score >= 0  # bm25 negated → higher better

    def test_namespace_filter(self, tmp_obscura_home: Path) -> None:
        repo = get_keyword_memory_repo()
        repo.remember("auth refactor done", namespace="project:obscura")
        repo.remember("auth notes from 2026", namespace="cli")

        all_hits = repo.recall("auth")
        project_only = repo.recall("auth", namespace="project:obscura")
        assert len(all_hits) == 2
        assert len(project_only) == 1
        assert project_only[0].namespace == "project:obscura"

    def test_list_by_namespace_prefix(self, tmp_obscura_home: Path) -> None:
        repo = get_keyword_memory_repo()
        repo.remember("name: Elliott", namespace="user:profile")
        repo.remember("prefers terse", namespace="user:prefs")
        repo.remember("project note", namespace="project:obscura")

        user_mems = repo.list_by_namespace_prefix("user")
        assert len(user_mems) == 2
        assert all(m.namespace.startswith("user") for m in user_mems)

    def test_forget(self, tmp_obscura_home: Path) -> None:
        repo = get_keyword_memory_repo()
        new_id = repo.remember("temporary note")
        assert repo.forget(new_id) is True
        assert repo.forget(99999) is False
        assert repo.recall("temporary") == []

    def test_stats(self, tmp_obscura_home: Path) -> None:
        repo = get_keyword_memory_repo()
        repo.remember("a", namespace="x")
        repo.remember("b", namespace="x")
        repo.remember("c", namespace="y")
        s = repo.stats()
        assert s["backend"] == "sqlite"
        assert s["total"] == 3
        assert s["namespaces"] == {"x": 2, "y": 1}

    def test_empty_query_returns_empty(self, tmp_obscura_home: Path) -> None:
        repo = get_keyword_memory_repo()
        repo.remember("anything")
        assert repo.recall("") == []

    def test_malformed_fts_query_returns_empty(
        self,
        tmp_obscura_home: Path,
    ) -> None:
        repo = get_keyword_memory_repo()
        repo.remember("anything")
        # Unbalanced quote — FTS5 would raise, repo should swallow + return [].
        assert repo.recall('"unterminated') == []

    def test_remember_rejects_empty_content(self, tmp_obscura_home: Path) -> None:
        repo = get_keyword_memory_repo()
        with pytest.raises(ValueError):
            repo.remember("")
        with pytest.raises(ValueError):
            repo.remember("   ")


@pytest.mark.unit
class TestProtocolConformance:
    def test_sqlite_repo_satisfies_protocol(self, tmp_obscura_home: Path) -> None:
        repo: KeywordMemoryRepo = get_keyword_memory_repo()
        assert isinstance(repo, KeywordMemoryRepo)

    def test_postgres_repo_class_has_required_methods(self) -> None:
        # Don't instantiate (no DB in test env) — verify class structure.
        from obscura.data.keyword_memory.postgres import PostgresKeywordMemoryRepo

        for method in (
            "remember",
            "recall",
            "forget",
            "list_namespaces",
            "list_by_namespace_prefix",
            "stats",
            "close",
        ):
            assert hasattr(PostgresKeywordMemoryRepo, method), method


@pytest.mark.unit
class TestFactory:
    def test_default_returns_sqlite(self, tmp_obscura_home: Path) -> None:
        repo = get_keyword_memory_repo()
        assert isinstance(repo, SqliteKeywordMemoryRepo)
        repo.close()

    def test_keyword_memory_available_when_no_file(
        self,
        tmp_obscura_home: Path,
    ) -> None:
        # Fresh dir, before any remember(): file doesn't exist yet.
        assert keyword_memory_available() is False

    def test_keyword_memory_available_after_first_write(
        self,
        tmp_obscura_home: Path,
    ) -> None:
        repo = get_keyword_memory_repo()
        repo.remember("seeded")
        assert keyword_memory_available() is True


@pytest.mark.unit
class TestMemoryDataclass:
    def test_to_dict_round_trip(self) -> None:
        m = Memory(
            id=1,
            namespace="user:profile",
            content="hello",
            metadata={"tag": "intro"},
            created_at=1.0,
            updated_at=2.0,
            score=0.42,
        )
        d = m.to_dict()
        assert d["id"] == 1
        assert d["namespace"] == "user:profile"
        assert d["score"] == 0.42
        assert d["metadata"] == {"tag": "intro"}
