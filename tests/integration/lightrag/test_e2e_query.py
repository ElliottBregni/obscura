"""End-to-end query test — real lightrag.aquery against a tiny cassette."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.lightrag_integration


@pytest.mark.vcr("cassettes/tiny_corpus_query.yaml")
async def test_query_recovers_relevant_doc_ids(real_lightrag) -> None:
    """After cassetted ingest, a query for 'Alice' returns the right docs."""
    hits = await real_lightrag.aquery("Who works with Alice?", mode="hybrid", top_k=5)
    assert len(hits) >= 1
    keys = [h.key for h in hits]
    assert any("doc2" in k or "bob" in k.lower() for k in keys)
