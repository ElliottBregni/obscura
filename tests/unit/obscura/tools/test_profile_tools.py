"""Unit tests for profile_* tools.

All six profile tools are **sync** functions, so tests use ``def test_*``.

Mocking strategy:
  - _profile_store() → None by default (no vector backend)
  - _profile()       → MagicMock with sensible defaults
  - _notify_vault_profile() → no-op
  - Tests that need the vector store use a _with_store fixture that
    supplies a configured MagicMock.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _suppress_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "obscura.tools.profile_tools._notify_vault_profile", lambda: None
    )


@pytest.fixture
def mock_profile(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock UserProfile with safe defaults."""
    p = MagicMock()
    p.exists.return_value = True
    p.read.return_value = "# Elliott\n\nLikes Python."
    p.active_summary.return_value = "Likes Python."
    p.append_fact.return_value = True
    p.semantic_recall.return_value = []
    p.sync_to_vector_store.return_value = 0
    monkeypatch.setattr("obscura.tools.profile_tools._profile", lambda: p)
    return p


@pytest.fixture
def mock_store(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock ProfileStore with standard return values."""
    store = MagicMock()
    store.get_all_facts.return_value = []
    store.set_fact.return_value = None
    store.forget.return_value = True
    # build_summary is on the builder, not the store
    monkeypatch.setattr("obscura.tools.profile_tools._profile_store", lambda: store)

    builder = MagicMock()
    builder.build_summary.return_value = "Vector summary text."
    monkeypatch.setattr("obscura.tools.profile_tools._profile_builder", lambda: builder)
    return store


@pytest.fixture
def no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("obscura.tools.profile_tools._profile_store", lambda: None)


# ---------------------------------------------------------------------------
# profile_get
# ---------------------------------------------------------------------------


def test_profile_get_no_store_falls_back_to_markdown(
    mock_profile: MagicMock, no_store: None
) -> None:
    from obscura.tools.profile_tools import profile_get

    result = json.loads(profile_get())

    assert result["ok"] is True
    assert result["source"] == "markdown"
    assert "Python" in result["summary"]


def test_profile_get_no_store_full_reads_file(
    mock_profile: MagicMock, no_store: None
) -> None:
    from obscura.tools.profile_tools import profile_get

    result = json.loads(profile_get(compact=False))

    assert result["ok"] is True
    assert "Elliott" in result["profile"]


def test_profile_get_no_profile_file_returns_error(
    monkeypatch: pytest.MonkeyPatch, no_store: None
) -> None:
    from obscura.tools.profile_tools import profile_get

    p = MagicMock()
    p.exists.return_value = False
    monkeypatch.setattr("obscura.tools.profile_tools._profile", lambda: p)

    result = json.loads(profile_get())

    assert result["ok"] is False
    assert result["error"] == "profile_not_found"


def test_profile_get_with_store_returns_vector_summary(
    mock_profile: MagicMock, mock_store: MagicMock
) -> None:
    from obscura.tools.profile_tools import profile_get

    result = json.loads(profile_get())

    assert result["ok"] is True
    assert result["source"] == "vector"
    assert "Vector summary" in result["summary"]


def test_profile_get_include_scores_returns_facts(
    mock_profile: MagicMock, mock_store: MagicMock
) -> None:
    from obscura.tools.profile_tools import profile_get
    from unittest.mock import MagicMock as MM

    fact = MM()
    fact.key = "career.lang"
    fact.value = "Python"
    fact.category = MM()
    fact.category.value = "career"
    fact.source = "user"
    mock_store.get_all_facts.return_value = [(fact, 0.95)]

    result = json.loads(profile_get(include_scores=True))

    assert result["ok"] is True
    assert result["source"] == "vector"
    assert result["count"] == 1
    assert result["facts"][0]["key"] == "career.lang"
    assert result["facts"][0]["score"] == 0.95


# ---------------------------------------------------------------------------
# profile_update
# ---------------------------------------------------------------------------


def test_profile_update_appends_fact(mock_profile: MagicMock, no_store: None) -> None:
    from obscura.tools.profile_tools import profile_update

    result = json.loads(profile_update(fact="I like Python"))

    assert result["ok"] is True
    assert result["appended"] is True
    assert result["fact"] == "I like Python"
    mock_profile.append_fact.assert_called_once()


def test_profile_update_duplicate_returns_not_appended(
    mock_profile: MagicMock, no_store: None
) -> None:
    from obscura.tools.profile_tools import profile_update

    mock_profile.append_fact.return_value = False
    result = json.loads(profile_update(fact="same fact"))

    assert result["ok"] is True
    assert result["appended"] is False
    assert result["reason"] == "duplicate"


def test_profile_update_calls_store_when_available(
    mock_profile: MagicMock, mock_store: MagicMock
) -> None:
    from obscura.tools.profile_tools import profile_update

    result = json.loads(profile_update(fact="uses vim"))

    assert result["ok"] is True
    mock_store.set_fact.assert_called_once()


def test_profile_update_invalid_memory_type_falls_back_to_fact(
    mock_profile: MagicMock, no_store: None
) -> None:
    from obscura.tools.profile_tools import profile_update

    # Invalid type silently falls back to "fact" (no error)
    result = json.loads(profile_update(fact="something", memory_type="garbage"))

    assert result["ok"] is True
    assert result["memory_type"] == "fact"


# ---------------------------------------------------------------------------
# profile_recall
# ---------------------------------------------------------------------------


def test_profile_recall_returns_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from obscura.tools.profile_tools import profile_recall

    p = MagicMock()
    p.semantic_recall.return_value = [
        {"text": "Likes Python", "score": 0.9},
        {"text": "Works remotely", "score": 0.8},
    ]
    monkeypatch.setattr("obscura.tools.profile_tools._profile", lambda: p)

    result = json.loads(profile_recall(query="programming"))

    assert result["ok"] is True
    assert result["count"] == 2
    assert result["results"][0]["text"] == "Likes Python"


def test_profile_recall_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from obscura.tools.profile_tools import profile_recall

    p = MagicMock()
    p.semantic_recall.return_value = []
    monkeypatch.setattr("obscura.tools.profile_tools._profile", lambda: p)

    result = json.loads(profile_recall(query="unknown topic"))

    assert result["ok"] is True
    assert result["count"] == 0
    assert result["results"] == []


def test_profile_recall_passes_top_k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from obscura.tools.profile_tools import profile_recall

    p = MagicMock()
    p.semantic_recall.return_value = []
    monkeypatch.setattr("obscura.tools.profile_tools._profile", lambda: p)

    profile_recall(query="q", top_k=3)

    p.semantic_recall.assert_called_once_with("q", top_k=3)


# ---------------------------------------------------------------------------
# profile_sync
# ---------------------------------------------------------------------------


def test_profile_sync_no_store_uses_legacy(
    mock_profile: MagicMock, no_store: None
) -> None:
    from obscura.tools.profile_tools import profile_sync

    mock_profile.sync_to_vector_store.return_value = 7
    result = json.loads(profile_sync())

    assert result["ok"] is True
    assert result["synced"] == 7
    assert result["method"] == "legacy"


def test_profile_sync_with_store_uses_migration(
    mock_profile: MagicMock, mock_store: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obscura.tools.profile_tools import profile_sync

    # Patch migrate_flat_profile to return a count without touching disk
    monkeypatch.setattr(
        "obscura.tools.profile_tools.migrate_flat_profile",
        lambda path, store: 5,
    )
    # Also patch Path.exists so the "user_profile.md" lookup resolves
    from pathlib import Path

    monkeypatch.setattr(Path, "exists", lambda self: True)

    result = json.loads(profile_sync())

    assert result["ok"] is True
    assert result["synced"] == 5
    assert result["method"] == "vector_migration"


# ---------------------------------------------------------------------------
# profile_set
# ---------------------------------------------------------------------------


def test_profile_set_stores_fact(mock_store: MagicMock) -> None:
    from obscura.tools.profile_tools import profile_set

    result = json.loads(
        profile_set(key="identity.name", value="Elliott", category="identity")
    )

    assert result["ok"] is True
    assert result["key"] == "identity.name"
    mock_store.set_fact.assert_called_once()


def test_profile_set_no_store_returns_error(no_store: None) -> None:
    from obscura.tools.profile_tools import profile_set

    result = json.loads(profile_set(key="k", value="v", category="identity"))

    assert result["ok"] is False


def test_profile_set_invalid_category_returns_error(mock_store: MagicMock) -> None:
    from obscura.tools.profile_tools import profile_set

    result = json.loads(profile_set(key="k", value="v", category="bogus_cat"))

    assert result["ok"] is False
    assert "Unknown category" in result["error"]


# ---------------------------------------------------------------------------
# profile_forget
# ---------------------------------------------------------------------------


def test_profile_forget_deletes_fact(mock_store: MagicMock) -> None:
    from obscura.tools.profile_tools import profile_forget

    mock_store.forget.return_value = True
    result = json.loads(profile_forget(key="old.fact"))

    assert result["ok"] is True
    assert result["deleted"] is True
    mock_store.forget.assert_called_once_with("old.fact")


def test_profile_forget_not_found_returns_error(mock_store: MagicMock) -> None:
    from obscura.tools.profile_tools import profile_forget

    mock_store.forget.return_value = False
    result = json.loads(profile_forget(key="ghost.key"))

    assert result["ok"] is False


def test_profile_forget_no_store_returns_error(no_store: None) -> None:
    from obscura.tools.profile_tools import profile_forget

    result = json.loads(profile_forget(key="any"))

    assert result["ok"] is False


# ---------------------------------------------------------------------------
# Tool spec registration
# ---------------------------------------------------------------------------


def test_get_profile_tool_specs_returns_six_specs() -> None:
    from obscura.tools.profile_tools import get_profile_tool_specs

    specs = get_profile_tool_specs()

    assert len(specs) == 6
    names = {s.name for s in specs}
    assert names == {
        "profile_get",
        "profile_update",
        "profile_recall",
        "profile_sync",
        "profile_set",
        "profile_forget",
    }
    for spec in specs:
        assert callable(spec.handler)
        assert isinstance(spec.parameters, dict)
