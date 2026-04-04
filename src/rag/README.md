Vertical RAG POC

This folder contains a minimal scaffold for the Vertical RAG proof-of-concept.

Files:
- __init__.py: package marker
- ingest.py: placeholder ingest pipeline (chunking/embedding/vector-store to be implemented)
- requirements.txt: Python dependencies for the POC

Getting started:
1. Create a venv: python -m venv .venv && source .venv/bin/activate
2. pip install -r src/rag/requirements.txt
3. Implement ingest -> embed -> store -> qa pipeline
