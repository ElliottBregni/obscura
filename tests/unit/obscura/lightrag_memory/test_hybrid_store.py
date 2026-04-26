"""Tests for HybridVectorMemoryStore overrides — set/delete/whitelist."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore

    from .conftest import MockLightRAG


def _wait_for(
    predicate: Callable[[], bool], timeout_s: float = 2.0, poll_s: float = 0.01
) -> bool:
    """Poll `predicate()` until truthy or timeout. Returns True if observed."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return False


class TestSetFanout:
    def test_set_calls_super_synchronously(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        """`set()` returns *before* `insert_safe` is called."""
        mock_lightrag.state.next_insert_sleep_s = 0.5

        t_start = time.monotonic()
        hybrid_store.set("k1", "x" * 50, memory_type="fact")
        elapsed = time.monotonic() - t_start

        assert elapsed < 0.2, f"set() blocked {elapsed:.3f}s — fan-out is sync"

        entry = hybrid_store.get("k1")
        assert entry is not None
        assert entry.text == "x" * 50

    def test_set_calls_adapter_for_indexable_type(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        """`memory_type="fact"` fan-outs to `insert_safe` within a timeout."""
        hybrid_store.set("k1", "User likes Python.", memory_type="fact")

        ok = _wait_for(lambda: len(mock_lightrag.state.inserts) >= 1, timeout_s=2.0)
        assert ok, "fan-out never landed"
        doc_id, text, metadata = mock_lightrag.state.inserts[0]
        assert "k1" in doc_id
        assert text == "User likes Python."
        assert metadata["memory_type"] == "fact"
        assert metadata.get("obscura_key") == "k1"
        assert metadata.get("obscura_namespace") == "default"

    def test_set_calls_adapter_for_summary(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        """`memory_type="summary"` is in the default whitelist."""
        hybrid_store.set(
            "s1",
            "Discussion summary about decay tuning.",
            memory_type="summary",
        )
        ok = _wait_for(lambda: len(mock_lightrag.state.inserts) >= 1)
        assert ok

    def test_set_skips_adapter_for_episode(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        """`memory_type="episode"` is NOT in the whitelist — no fan-out."""
        hybrid_store.set("e1", "User said hello.", memory_type="episode")
        time.sleep(0.1)
        assert mock_lightrag.state.inserts == []

    def test_set_skips_adapter_for_preference(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        """`preference` is structured KV, not graph material."""
        hybrid_store.set("p1", "User prefers concise output.", memory_type="preference")
        time.sleep(0.1)
        assert mock_lightrag.state.inserts == []

    def test_metadata_override_force_index(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        """`metadata={"graph_index": True}` overrides the whitelist for episodes."""
        hybrid_store.set(
            "e2",
            "Important episode worth indexing.",
            memory_type="episode",
            metadata={"graph_index": True},
        )
        ok = _wait_for(lambda: len(mock_lightrag.state.inserts) >= 1)
        assert ok

    def test_metadata_override_skip_index(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        """`metadata={"graph_index": False}` opts out even for `fact`."""
        hybrid_store.set(
            "f2",
            "A fact we don't want in the graph.",
            memory_type="fact",
            metadata={"graph_index": False},
        )
        time.sleep(0.1)
        assert mock_lightrag.state.inserts == []

    def test_short_text_skipped(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        """Text below `MIN_LENGTH` skips fan-out (avoids LLM cost on chatter)."""
        hybrid_store.set("k3", "ok", memory_type="fact")
        time.sleep(0.1)
        for _, text, metadata in mock_lightrag.state.inserts:
            assert metadata.get("_skip_reason") == "too_short" or len(text) >= 20


class TestDelete:
    def test_delete_propagates_to_adapter(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        """Deleting a key triggers `delete_safe(doc_id)` on the adapter."""
        hybrid_store.set("k1", "Content to be deleted.", memory_type="fact")
        _wait_for(lambda: len(mock_lightrag.state.inserts) >= 1)

        deleted = hybrid_store.delete("k1")
        assert deleted is True
        ok = _wait_for(lambda: len(mock_lightrag.state.deletes) >= 1)
        assert ok
        assert "k1" in mock_lightrag.state.deletes[0]

    def test_delete_swallows_adapter_errors(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If `delete_safe` raises, `delete()` still returns True for the chunk."""

        def _boom(doc_id: str) -> None:
            raise RuntimeError("simulated graph delete failure")

        monkeypatch.setattr(mock_lightrag, "delete_safe", _boom)
        hybrid_store.set("k1", "Content.", memory_type="fact")
        _wait_for(lambda: len(mock_lightrag.state.inserts) >= 1)
        deleted = hybrid_store.delete("k1")
        assert deleted is True

    def test_delete_missing_key_returns_false(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        deleted = hybrid_store.delete("does-not-exist")
        assert deleted is False
        time.sleep(0.05)
        assert mock_lightrag.state.deletes == []


class TestDocIdRoundtrip:
    def test_doc_id_decodes_unambiguously(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        """Encoded `f"{namespace}::{key}"` round-trips."""
        hybrid_store.set(
            "user_lang_python",
            "User uses Python.",
            namespace="default",
            memory_type="fact",
        )
        _wait_for(lambda: len(mock_lightrag.state.inserts) >= 1)
        doc_id, _, _ = mock_lightrag.state.inserts[0]
        ns, _, key = doc_id.partition("::")
        assert ns == "default"
        assert key == "user_lang_python"

    def test_doc_id_handles_namespace_with_colon(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        """Namespaces using single `:` (e.g. `default:semantic`) decode correctly."""
        hybrid_store.set(
            "k1",
            "test content for ns:colon",
            namespace="default:semantic",
            memory_type="fact",
        )
        _wait_for(lambda: len(mock_lightrag.state.inserts) >= 1)
        doc_id, _, _ = mock_lightrag.state.inserts[0]
        ns, sep, key = doc_id.partition("::")
        assert sep == "::"
        assert ns == "default:semantic"
        assert key == "k1"

    def test_namespace_containing_double_colon_documented(self) -> None:
        """Namespaces MUST NOT contain `::` — the doc_id encoding assumes this."""
        from obscura.lightrag_memory.hybrid_store import _decode_doc_id, _encode_doc_id

        encoded = _encode_doc_id("default", "k1")
        assert _decode_doc_id(encoded) == ("default", "k1")

        with pytest.raises((ValueError, AssertionError)):
            _encode_doc_id("bad::namespace", "k1")
