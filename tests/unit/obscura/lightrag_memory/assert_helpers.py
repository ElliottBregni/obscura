"""Reusable assertion utilities for the lightrag_memory test suite."""

from __future__ import annotations

from typing import Any


def assert_score_decreasing(results: list[Any]) -> None:
    """Assert that `final_score` is monotonically non-increasing."""
    scores = [getattr(r, "final_score", 0.0) for r in results]
    for i in range(len(scores) - 1):
        assert scores[i] >= scores[i + 1], (
            f"results not sorted by final_score at index {i}: "
            f"{scores[i]:.4f} < {scores[i + 1]:.4f}"
        )


def assert_metadata_subset(
    actual: dict[str, Any], expected_subset: dict[str, Any]
) -> None:
    """Assert that every (k, v) in `expected_subset` is present in `actual`."""
    missing = {k: v for k, v in expected_subset.items() if actual.get(k) != v}
    assert not missing, (
        f"metadata subset mismatch: expected {expected_subset!r}, "
        f"actual {actual!r}, missing/wrong: {missing!r}"
    )


def assert_doc_id_format(doc_id: str) -> None:
    """Doc IDs are `f"{namespace}::{key}"`. Assert basic shape."""
    assert "::" in doc_id, f"doc_id missing `::` separator: {doc_id!r}"
    parts = doc_id.split("::", 1)
    assert len(parts) == 2 and all(parts), f"malformed doc_id: {doc_id!r}"
