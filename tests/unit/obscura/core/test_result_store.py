"""Tests for obscura.core.result_store — large result persistence."""

from __future__ import annotations

import os

import pytest

from obscura.core.result_store import ResultStore


@pytest.fixture
def store(tmp_path: object) -> ResultStore:
    """ResultStore backed by a temp directory."""
    return ResultStore(base_dir=str(tmp_path), threshold=100)


class TestResultStore:
    def test_small_result_passes_through(self, store: ResultStore) -> None:
        text = "small result"
        preview, path = store.maybe_persist("call-1", text)
        assert preview == text
        assert path is None

    def test_exact_threshold_passes_through(self, store: ResultStore) -> None:
        text = "x" * 100
        preview, path = store.maybe_persist("call-2", text)
        assert preview == text
        assert path is None

    def test_large_result_persisted(self, store: ResultStore) -> None:
        text = "y" * 200
        preview, path = store.maybe_persist("call-3", text)
        assert path is not None
        assert os.path.exists(path)
        # Preview contains the first 100 chars (threshold) + footer
        assert preview.startswith("y" * 100)
        assert "truncated" in preview
        assert "200 chars total" in preview
        assert path in preview

    def test_persisted_content_matches(self, store: ResultStore) -> None:
        text = "z" * 500
        _, path = store.maybe_persist("call-4", text)
        assert path is not None
        content = ResultStore.read_full(path)
        assert content == text

    def test_preview_starts_with_original(self, store: ResultStore) -> None:
        text = "abcdef" * 50  # 300 chars, threshold=100
        preview, _ = store.maybe_persist("call-5", text)
        assert preview.startswith(text[:100])

    def test_safe_filename(self, store: ResultStore) -> None:
        text = "x" * 200
        _, path = store.maybe_persist("call/with:bad<chars>", text)
        assert path is not None
        assert os.path.exists(path)
        # Unsafe chars should be replaced with underscores
        assert "/" not in os.path.basename(path).replace(".txt", "").replace("_", "")

    def test_directory_created_on_first_write(self, tmp_path: object) -> None:
        new_dir = os.path.join(str(tmp_path), "nested", "dir")
        store = ResultStore(base_dir=new_dir, threshold=10)
        text = "x" * 50
        _, path = store.maybe_persist("call-6", text)
        assert path is not None
        assert os.path.isdir(new_dir)

    def test_idempotent_persist(self, store: ResultStore) -> None:
        text = "x" * 200
        _, path1 = store.maybe_persist("call-7", text)
        _, path2 = store.maybe_persist("call-7", text)
        assert path1 == path2

    def test_default_threshold(self, tmp_path: object) -> None:
        store = ResultStore(base_dir=str(tmp_path))
        # Default threshold is 200_000
        small = "x" * 199_999
        _, path = store.maybe_persist("call-8", small)
        assert path is None

        large = "x" * 200_001
        _, path = store.maybe_persist("call-9", large)
        assert path is not None
