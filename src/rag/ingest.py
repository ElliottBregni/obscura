"""Ingest module

This module provides a minimal ingest pipeline placeholder for the Vertical RAG POC.
Replace with real ingestion, chunking, embedding, and vector-store logic.
"""

from typing import Iterable


def ingest_documents(docs: Iterable[str]) -> list:
    """Return list of "document records" as a placeholder.

    Args:
        docs: iterable of plain-text documents

    Returns:
        list of dicts with id and text
    """
    out = []
    for i, d in enumerate(docs):
        out.append({"id": f"doc_{i}", "text": d})
    return out


if __name__ == "__main__":
    sample = ["Example doc 1", "Example doc 2"]
    print(ingest_documents(sample))
