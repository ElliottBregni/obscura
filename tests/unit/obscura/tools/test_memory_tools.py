"""Unit tests for memory_tools — store_memory, recall_memory, semantic_search,
store_searchable, and build_channels_prompt_section.

All four handler closures are sync.  Tests mock:
  - create_memory_store    → MagicMock KV store
  - VectorMemoryStore.for_user → MagicMock vector store
so no real Qdrant or disk I/O occurs.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, call

import pytest

import obscura.tools.memory_tools as _mem_mod

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_user() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_kv_store(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    store = MagicMock()
    store.get.return_value = None
    monkeypatch.setattr(_mem_mod, "create_memory_store", lambda _: store)
    return store


@pytest.fixture
def mock_vector_store(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    store = MagicMock()
    store.search_similar.return_value = []
    monkeypatch.setattr(
        _mem_mod.VectorMemoryStore, "for_user", staticmethod(lambda _: store)
    )
    return store


def _get_handlers(user: MagicMock) -> dict:  # type: ignore[type-arg]
    from obscura.tools.memory_tools import make_memory_tool_specs

    return {s.name: s.handler for s in make_memory_tool_specs(user)}


# ---------------------------------------------------------------------------
# make_memory_tool_specs — spec structure
# ---------------------------------------------------------------------------


def test_make_memory_tool_specs_returns_four_specs(mock_user: MagicMock) -> None:
    from obscura.tools.memory_tools import make_memory_tool_specs

    specs = make_memory_tool_specs(mock_user)

    assert len(specs) == 4
    names = {s.name for s in specs}
    assert names == {"store_memory", "recall_memory", "semantic_search", "store_searchable"}
    for spec in specs:
        assert callable(spec.handler)
        assert isinstance(spec.parameters, dict)
        assert spec.parameters.get("type") == "object"


# ---------------------------------------------------------------------------
# store_memory
# ---------------------------------------------------------------------------


def test_store_memory_calls_kv_set(
    mock_user: MagicMock, mock_kv_store: MagicMock
) -> None:
    result = json.loads(
        _get_handlers(mock_user)["store_memory"](
            namespace="ns", key="k", value={"x": 1}
        )
    )

    assert result["ok"] is True
    assert result["namespace"] == "ns"
    assert result["key"] == "k"
    mock_kv_store.set.assert_called_once_with(namespace="ns", key="k", value={"x": 1})


def test_store_memory_returns_value_keys(
    mock_user: MagicMock, mock_kv_store: MagicMock
) -> None:
    result = json.loads(
        _get_handlers(mock_user)["store_memory"](
            namespace="ns", key="k", value={"a": 1, "b": 2}
        )
    )

    assert set(result["value_keys"]) == {"a", "b"}


# ---------------------------------------------------------------------------
# recall_memory
# ---------------------------------------------------------------------------


def test_recall_memory_not_found(
    mock_user: MagicMock, mock_kv_store: MagicMock
) -> None:
    mock_kv_store.get.return_value = None
    result = json.loads(
        _get_handlers(mock_user)["recall_memory"](namespace="ns", key="missing")
    )

    assert result["ok"] is True
    assert result["found"] is False
    assert result["value"] is None


def test_recall_memory_found(mock_user: MagicMock, mock_kv_store: MagicMock) -> None:
    mock_kv_store.get.return_value = {"foo": "bar"}
    result = json.loads(
        _get_handlers(mock_user)["recall_memory"](namespace="ns", key="existing")
    )

    assert result["ok"] is True
    assert result["found"] is True
    assert result["value"]["foo"] == "bar"


# ---------------------------------------------------------------------------
# semantic_search
# ---------------------------------------------------------------------------


def test_semantic_search_no_results(
    mock_user: MagicMock, mock_vector_store: MagicMock
) -> None:
    result = json.loads(
        _get_handlers(mock_user)["semantic_search"](query="anything")
    )

    assert result["ok"] is True
    assert result["count"] == 0
    assert result["results"] == []


def _make_vector_entry(text: str = "hello world", ns: str = "ns") -> MagicMock:
    entry = MagicMock()
    entry.key = MagicMock()
    entry.key.__str__ = lambda self: f"{ns}:k"
    entry.key.namespace = ns
    entry.score = 0.9
    entry.final_score = 0.85
    entry.text = text
    entry.memory_type = "fact"
    entry.metadata = {}
    return entry


def test_semantic_search_with_explicit_namespace_returns_results(
    mock_user: MagicMock, mock_vector_store: MagicMock
) -> None:
    mock_vector_store.search_similar.return_value = [_make_vector_entry()]

    result = json.loads(
        _get_handlers(mock_user)["semantic_search"](query="hello", namespace="testns")
    )

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["results"][0]["text"] == "hello world"
    mock_vector_store.search_similar.assert_called_once_with(
        "hello", namespace="testns", top_k=5
    )


def test_semantic_search_uses_project_namespace_when_none(
    mock_user: MagicMock,
    mock_vector_store: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When namespace is omitted, first call uses the project namespace."""
    monkeypatch.setattr(_mem_mod, "_project_namespace", lambda: "project:test")
    mock_vector_store.search_similar.return_value = []

    _get_handlers(mock_user)["semantic_search"](query="q")

    calls = mock_vector_store.search_similar.call_args_list
    # Two calls: project namespace first, then None (global)
    assert len(calls) == 2
    assert calls[0] == call("q", namespace="project:test", top_k=5)
    assert calls[1] == call("q", namespace=None, top_k=5)


def test_semantic_search_deduplicates_project_and_global_results(
    mock_user: MagicMock,
    mock_vector_store: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same entry returned from both calls must appear only once."""
    monkeypatch.setattr(_mem_mod, "_project_namespace", lambda: "project:x")
    entry = _make_vector_entry()
    mock_vector_store.search_similar.return_value = [entry]

    result = json.loads(
        _get_handlers(mock_user)["semantic_search"](query="q")
    )

    # Both proj + global return same entry key → deduplicated to 1
    assert result["count"] == 1


# ---------------------------------------------------------------------------
# store_searchable
# ---------------------------------------------------------------------------


def test_store_searchable_calls_vector_set(
    mock_user: MagicMock, mock_vector_store: MagicMock
) -> None:
    result = json.loads(
        _get_handlers(mock_user)["store_searchable"](key="k", text="hello")
    )

    assert result["ok"] is True
    assert result["key"] == "k"
    mock_vector_store.set.assert_called_once()


def test_store_searchable_defaults_to_project_namespace(
    mock_user: MagicMock,
    mock_vector_store: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_mem_mod, "_project_namespace", lambda: "project:myrepo")

    result = json.loads(
        _get_handlers(mock_user)["store_searchable"](key="k", text="data", namespace="")
    )

    assert result["ok"] is True
    assert result["namespace"] == "project:myrepo"


def test_store_searchable_explicit_namespace_used(
    mock_user: MagicMock, mock_vector_store: MagicMock
) -> None:
    result = json.loads(
        _get_handlers(mock_user)["store_searchable"](
            key="k", text="data", namespace="custom:ns"
        )
    )

    assert result["namespace"] == "custom:ns"


def test_store_searchable_returns_text_length(
    mock_user: MagicMock, mock_vector_store: MagicMock
) -> None:
    result = json.loads(
        _get_handlers(mock_user)["store_searchable"](key="k", text="hello")
    )

    assert result["text_length"] == 5


def test_store_searchable_memory_type_passed_through(
    mock_user: MagicMock, mock_vector_store: MagicMock
) -> None:
    result = json.loads(
        _get_handlers(mock_user)["store_searchable"](
            key="k", text="data", memory_type="fact"
        )
    )

    assert result["ok"] is True
    assert result["memory_type"] == "fact"


# ---------------------------------------------------------------------------
# build_channels_prompt_section
# ---------------------------------------------------------------------------


def test_build_channels_prompt_section_empty_returns_empty_string() -> None:
    from obscura.tools.memory_tools import build_channels_prompt_section

    assert build_channels_prompt_section([]) == ""


def test_build_channels_prompt_section_with_channel_includes_name_and_namespace() -> None:
    from obscura.tools.memory_tools import build_channels_prompt_section

    ch = MagicMock()
    ch.name = "architecture"
    ch.namespace = "workspace:arch"
    ch.priority = 10
    ch.injection = "system"
    ch.triggers.always = True
    ch.triggers.file_globs = []
    ch.triggers.keywords = []
    ch.triggers.tool_names = []

    result = build_channels_prompt_section([ch])

    assert "Memory Channels" in result
    assert "architecture" in result
    assert "workspace:arch" in result
    assert "store_searchable" in result
