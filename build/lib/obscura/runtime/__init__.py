"""obscura.runtime — Optional, lazy-loadable runtime modules.

Anything in this namespace may pull heavy SDK chains (psycopg, qdrant, etc.)
or be runtime-only concerns (storage adapters, caches, predictive helpers,
default data). Code in :mod:`obscura.core` must NEVER eagerly import from
here — that would defeat the lazy public API guarantee in ``obscura/__init__.py``.

Subpackages:

- :mod:`obscura.runtime.cache` — LLM and prompt caches
- :mod:`obscura.runtime.storage` — Postgres event store + adapters
- (top-level) — single-file runtime helpers
"""
