"""Tests for LightRAGAdapter — the real adapter, with a stubbed LightRAG."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest


class _FakeLightRAG:
    """Tiny stand-in for `lightrag.LightRAG` — async methods only."""

    def __init__(self) -> None:
        self.inserts: list[tuple[str, dict[str, Any]]] = []
        self.deletes: list[str] = []
        self.queries: list[tuple[str, str, int]] = []
        self.next_insert_raises: BaseException | None = None
        self.next_insert_sleep_s: float | None = None
        self.next_query_raises: BaseException | None = None

    async def ainsert(
        self,
        text: str,
        ids: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self.next_insert_sleep_s is not None:
            await asyncio.sleep(self.next_insert_sleep_s)
            self.next_insert_sleep_s = None
        if self.next_insert_raises is not None:
            exc = self.next_insert_raises
            self.next_insert_raises = None
            raise exc
        self.inserts.append((ids or "", metadata or {}))

    async def adelete_by_doc_id(self, doc_id: str) -> None:
        self.deletes.append(doc_id)

    async def aquery(self, query: str, param: Any) -> Any:
        if self.next_query_raises is not None:
            exc = self.next_query_raises
            self.next_query_raises = None
            raise exc
        self.queries.append(
            (query, getattr(param, "mode", "?"), getattr(param, "top_k", -1))
        )
        return "stub answer"


class TestAdapterConstruction:
    def test_clean_error_when_lightrag_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_user,
    ) -> None:
        """If `lightrag` isn't installed, the adapter raises a controlled error."""
        from obscura.lightrag_memory import adapter as adapter_mod

        monkeypatch.setattr(adapter_mod, "_lightrag_enabled", lambda: False)
        with pytest.raises(ImportError, match="lightrag"):
            adapter_mod.LightRAGAdapter.for_user(fake_user, embedding_fn=None)

    def test_construction_succeeds_with_stubbed_lightrag(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_user,
        tmp_path,
    ) -> None:
        """With a stubbed factory, `for_user` builds an adapter and creates working_dir."""
        from obscura.lightrag_memory import adapter as adapter_mod

        fake = _FakeLightRAG()
        monkeypatch.setattr(adapter_mod, "_lightrag_enabled", lambda: True)
        monkeypatch.setattr(
            adapter_mod, "_build_lightrag_instance", lambda **kwargs: fake
        )
        monkeypatch.setattr(adapter_mod, "_lightrag_root", lambda: tmp_path / "lr")

        adapter = adapter_mod.LightRAGAdapter.for_user(
            fake_user, embedding_fn=lambda s: [0.0] * 384
        )
        assert adapter is not None
        assert (tmp_path / "lr").exists()
        adapter.close()


class TestInsertSafe:
    def _adapter_with_fake(
        self, monkeypatch, fake_user, tmp_path
    ) -> tuple[Any, _FakeLightRAG]:
        from obscura.lightrag_memory import adapter as adapter_mod

        fake = _FakeLightRAG()
        monkeypatch.setattr(adapter_mod, "_lightrag_enabled", lambda: True)
        monkeypatch.setattr(
            adapter_mod, "_build_lightrag_instance", lambda **kwargs: fake
        )
        monkeypatch.setattr(adapter_mod, "_lightrag_root", lambda: tmp_path / "lr")

        adapter = adapter_mod.LightRAGAdapter.for_user(
            fake_user, embedding_fn=lambda s: [0.0] * 384
        )
        return adapter, fake

    def test_insert_safe_swallows_exceptions(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_user,
        tmp_path,
    ) -> None:
        """If `ainsert` raises, `insert_safe` does NOT propagate."""
        adapter, fake = self._adapter_with_fake(monkeypatch, fake_user, tmp_path)
        try:
            fake.next_insert_raises = RuntimeError("kaboom")
            adapter.insert_safe(doc_id="k1", text="some text" * 10, metadata={})
            assert adapter.failed_inserts >= 1
        finally:
            adapter.close()

    def test_insert_safe_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_user,
        tmp_path,
    ) -> None:
        """If `ainsert` hangs past the timeout, `insert_safe` cancels and logs."""
        adapter, fake = self._adapter_with_fake(monkeypatch, fake_user, tmp_path)
        try:
            monkeypatch.setattr(adapter, "ingest_timeout_s", 0.1)
            fake.next_insert_sleep_s = 1.0
            t0 = time.monotonic()
            adapter.insert_safe(doc_id="k1", text="text" * 30, metadata={})
            elapsed = time.monotonic() - t0
            assert elapsed < 0.5, f"insert_safe didn't time out — took {elapsed:.2f}s"
            assert adapter.failed_inserts >= 1
        finally:
            adapter.close()

    def test_concurrent_inserts_serialized_per_user(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_user,
        tmp_path,
    ) -> None:
        """Multiple `insert_safe` calls dispatched to the adapter's loop preserve order."""
        adapter, fake = self._adapter_with_fake(monkeypatch, fake_user, tmp_path)
        try:
            for i in range(10):
                adapter.insert_safe(
                    doc_id=f"k{i}", text=f"content {i}" * 5, metadata={"i": i}
                )
            deadline = time.monotonic() + 5.0
            while len(fake.inserts) < 10 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert len(fake.inserts) == 10
            ids = [doc_id for doc_id, _ in fake.inserts]
            assert ids == [f"k{i}" for i in range(10)]
        finally:
            adapter.close()


class TestClose:
    def test_close_drains_executor(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_user,
        tmp_path,
    ) -> None:
        """Submit 10 inserts, call close — they all complete or are cancelled cleanly."""
        from obscura.lightrag_memory import adapter as adapter_mod

        fake = _FakeLightRAG()
        monkeypatch.setattr(adapter_mod, "_lightrag_enabled", lambda: True)
        monkeypatch.setattr(
            adapter_mod, "_build_lightrag_instance", lambda **kwargs: fake
        )
        monkeypatch.setattr(adapter_mod, "_lightrag_root", lambda: tmp_path / "lr")
        adapter = adapter_mod.LightRAGAdapter.for_user(
            fake_user, embedding_fn=lambda s: [0.0] * 384
        )

        for i in range(10):
            adapter.insert_safe(doc_id=f"k{i}", text=f"content{i}" * 5, metadata={})
        t0 = time.monotonic()
        adapter.close()
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"close() blocked {elapsed:.2f}s"

    def test_close_idempotent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_user,
        tmp_path,
    ) -> None:
        from obscura.lightrag_memory import adapter as adapter_mod

        fake = _FakeLightRAG()
        monkeypatch.setattr(adapter_mod, "_lightrag_enabled", lambda: True)
        monkeypatch.setattr(
            adapter_mod, "_build_lightrag_instance", lambda **kwargs: fake
        )
        monkeypatch.setattr(adapter_mod, "_lightrag_root", lambda: tmp_path / "lr")
        adapter = adapter_mod.LightRAGAdapter.for_user(
            fake_user, embedding_fn=lambda s: [0.0] * 384
        )
        adapter.close()
        adapter.close()
        adapter.close()
