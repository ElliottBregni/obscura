"""End-to-end ingest test — real lightrag.ainsert against a tiny cassette."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.lightrag_integration


@pytest.mark.vcr("cassettes/tiny_corpus_ingest.yaml")
def test_ingest_three_documents_builds_graph(real_lightrag) -> None:
    """Ingest 3 small docs with predetermined entities; assert graph contents."""
    docs = [
        ("doc1", "Alice manages a team of three engineers in Berlin.", {}),
        ("doc2", "Bob works with Alice on the LightRAG migration.", {}),
        ("doc3", "Carol reviews Bob's pull requests in the LightRAG repo.", {}),
    ]
    for doc_id, text, metadata in docs:
        real_lightrag.insert_safe(doc_id, text, metadata)

    real_lightrag.flush()

    graph = real_lightrag._lightrag.chunk_entity_relation_graph._graph

    nodes = {str(n).lower() for n in graph.nodes()}
    assert any("alice" in n for n in nodes)
    assert any("bob" in n for n in nodes)
    assert graph.number_of_edges() >= 1
