# Phase 1 — Dependency + Scaffold

> **Status:** ready to execute.
> **Owner:** Elliott Bregni (`bregnie34@gmail.com`)
> **Drafted:** 2026-04-26
> **Predecessor:** [`00-overview.md`](./00-overview.md) §"Phase 1 — Dependency + scaffold"
> **Successor:** Phase 2 will wire `HybridVectorMemoryStore.set()` for fan-out ingest.

This document is implementation-ready. An engineer should be able to land a single PR from it without further design questions. All file paths are absolute on first introduction, then relative.

---

## 1. Goal & non-goals

### Goal

Land the **scaffolding** for the LightRAG integration so that subsequent phases have a stable surface to build on. After this phase:

1. `uv sync --extra lightrag` installs `lightrag-hku` and `networkx`.
2. The package `obscura.lightrag_memory` exists and is importable.
3. `LightRAGAdapter` can be instantiated for a user — it constructs a real `LightRAG` instance pointing at the user's Qdrant collection and an isolated `working_dir`. It exposes `insert_safe`, `delete_safe`, and `aquery` — the first two are sync-callable façades, the third is async.
4. `HybridVectorMemoryStore` exists as an empty subclass of `VectorMemoryStore` (the real overrides land in Phase 2/3).
5. Pure-math `hybrid_score()` + `HybridWeights` dataclass + a config-toml reader are in place and unit-tested.
6. `_lightrag_enabled()` is the **single** feature-flag check for the entire integration. It returns `False` by default and is the only place `import lightrag` is attempted.
7. Empty stubs for `ingest.py` (Phase 2) and `backfill.py` (Phase 5) keep import paths stable.

### Non-goals (explicit)

This phase deliberately does **not**:

- Change the behavior of any existing caller. `VectorMemoryStore.for_user()` continues to return a vanilla `VectorMemoryStore`. We do **not** modify `vector_memory.py:306`.
- Add new tools (`memory_graph_query`, `memory_graph_explain` are Phase 4).
- Implement `aquery` end-to-end — it's a placeholder that calls into `lightrag.aquery` but the result-hydration path (`GraphHit` → `VectorEntry`) lands in Phase 3.
- Implement write fan-out (`HybridVectorMemoryStore.set()` override is Phase 2).
- Implement backfill (Phase 5).
- Touch the `VectorBackend` protocol or any storage schema. No migrations.
- Add a new system-prompt section. No model-facing change.
- Force users to install the extra. With `OBSCURA_LIGHTRAG` unset and the extra not installed, the test suite must continue to pass identically.

The **single shipping behavior change** in Phase 1: with `OBSCURA_LIGHTRAG=on` AND the extra installed, *nothing happens yet at the call sites*, but `LightRAGAdapter.for_user(user)` works and constructs the underlying LightRAG instance. That gives Phase 2 a known-good adapter to call into.

---

## 2. Acceptance criteria

Concrete checks. Each must pass before the PR merges.

1. **Extras install.** `uv sync --extra lightrag` exits 0. `python -c "import lightrag, networkx; print(lightrag.__version__)"` prints a version ≥ 1.4.
2. **Scaffold imports without the extra.** With `lightrag` *not* installed:
   - `python -c "from obscura.lightrag_memory import _lightrag_enabled; print(_lightrag_enabled())"` prints `False`.
   - `python -c "from obscura.lightrag_memory.scoring import hybrid_score, HybridWeights"` exits 0.
   - `python -c "from obscura.lightrag_memory.adapter import LightRAGAdapter"` raises a controlled `ImportError` whose message names the missing extra (`pip install obscura[lightrag]`), **not** the bare `ModuleNotFoundError: No module named 'lightrag'`.
3. **Scaffold imports with the extra.** With `lightrag` installed and `OBSCURA_LIGHTRAG=on`:
   - `python -c "from obscura.lightrag_memory.adapter import LightRAGAdapter"` exits 0.
   - `LightRAGAdapter.for_user(test_user, _make_default_embedding_fn())` succeeds and creates `~/.obscura/lightrag/<user_hash>/` on disk.
4. **Existing test suite is untouched.** `pytest tests/ -m "not e2e"` produces the same pass count as `main` immediately before this PR. Specifically: `tests/unit/obscura/vector_memory/` runs without importing `obscura.lightrag_memory`.
5. **New unit tests pass.** `pytest tests/unit/obscura/lightrag_memory/ -v` runs four tests (see §8) and they all pass without `lightrag` installed in the dev environment.
6. **No accidental wiring.** `grep -r "lightrag_memory" obscura/` returns matches **only** in `obscura/lightrag_memory/` itself. No production code path imports it yet.
7. **`pyright`** passes on the new package: `pyright obscura/lightrag_memory/` exits 0.
8. **`ruff`** passes: `ruff check obscura/lightrag_memory/ tests/unit/obscura/lightrag_memory/` exits 0.

---

## 3. File-level changes

Seven files change. One pyproject edit, six new Python files (five in the package + one test file). No existing Python source under `obscura/` is modified.

### 3.1 `/Users/elliottbregni/dev/obscura-main/pyproject.toml`

This repo uses **PEP 621 `[project.optional-dependencies]`** for installable extras (lines 35-129) and a separate **`[dependency-groups]`** for dev tooling only (lines 137-151). Per the existing convention (one extra per provider/plugin, plus category bundles), `lightrag` belongs as a top-level optional extra alongside `voice`, `server`, `telemetry`, etc.

Add the new extra in the "Provider extras" or "Plugin extras" section. It's neither, strictly — it's an infra extra like `voice`. Place it in the **Infra extras** block right after `toml = [...]` (line 82), before the "Provider extras" header.

**Diff:**

```toml
toml = [
    "tomli-w>=1.0",
]

# LightRAG graph-aware retrieval. Optional layer on top of vector_memory.
# Enabled via OBSCURA_LIGHTRAG=on at runtime; safe to leave uninstalled.
lightrag = [
    "lightrag-hku>=1.4",
    "networkx>=3.0",
]

# ---------------------------------------------------------------------------
# Provider extras — one per non-default LLM/backend
# ---------------------------------------------------------------------------
anthropic = ["anthropic>=0.40.0"]
```

Also add `lightrag` to the `full` meta bundle (line 123) so the "batteries included" target picks it up:

```toml
full = ["obscura[server,telemetry,a2a,metrics,voice,anthropic,lightrag,plugins-all]"]
```

Do **not** add `lightrag` to the `providers` back-compat alias (line 127) — it's not a provider.

The `[tool.setuptools] packages` list (lines 154-191) must include the new package so `setuptools` ships it in sdist/wheel:

```toml
    "obscura.vector_memory",
    "obscura.lightrag_memory",   # <-- new line
    "obscura.telemetry",
```

That's the entire pyproject change. No change to `[dependency-groups]`, `[tool.ruff]`, or `[tool.pytest.ini_options]`.

### 3.2 `/Users/elliottbregni/dev/obscura-main/obscura/lightrag_memory/__init__.py`

Two responsibilities:

1. Hold the **single** feature-flag check (`_lightrag_enabled`).
2. Re-export pure-math symbols (`hybrid_score`, `HybridWeights`, `load_hybrid_weights_from_disk`) that have no `lightrag` dependency, so callers can use them safely whether the extra is installed or not.

**Crucially, do not eagerly import `adapter`, `hybrid_store`, `ingest`, or `backfill` from `__init__.py`.** Those modules touch the optional dep. Importing them at package-init time would crash the suite when `lightrag` isn't installed.

```python
"""obscura.lightrag_memory — LightRAG graph-aware retrieval layer.

This package sits behind the ``OBSCURA_LIGHTRAG`` feature flag. When disabled
(the default) it is inert — importing this module does not import LightRAG
and does not touch disk.

Public surface (stable across phases):

- :func:`_lightrag_enabled` — single source of truth for whether the layer is on.
- :class:`HybridWeights` and :func:`hybrid_score` — pure-math, no IO, safe to use
  without the optional extra installed.
- :func:`load_hybrid_weights_from_disk` — read the ``[vector_memory.lightrag.weights]``
  section of ``~/.obscura/config.toml``.

The heavy modules (:mod:`obscura.lightrag_memory.adapter`,
:mod:`obscura.lightrag_memory.hybrid_store`) are imported lazily by callers
gated behind :func:`_lightrag_enabled`, so they never run unless the user
opts in AND ``lightrag-hku`` is installed.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from obscura.lightrag_memory.scoring import (
    HybridWeights,
    hybrid_score,
    load_hybrid_weights,
    load_hybrid_weights_from_disk,
)

__all__ = [
    "HybridWeights",
    "_lightrag_enabled",
    "hybrid_score",
    "load_hybrid_weights",
    "load_hybrid_weights_from_disk",
]

_log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _lightrag_enabled() -> bool:
    """Return True iff the LightRAG layer is both *requested* and *available*.

    Precedence (first hit wins):
    1. ``OBSCURA_LIGHTRAG`` environment variable: ``on`` / ``1`` / ``true`` → True;
       ``off`` / ``0`` / ``false`` → False.
    2. ``[vector_memory.lightrag] enabled = true`` in ``~/.obscura/config.toml``.
    3. Default: ``False``.

    Even when requested, this returns False (with a one-time warning) if the
    ``lightrag`` package cannot be imported. That makes it safe to set
    ``OBSCURA_LIGHTRAG=on`` on a machine that doesn't have the extra
    installed — the caller silently falls back to the vanilla store.

    Cached for the life of the process via ``lru_cache`` because hot paths
    will check this on every call to ``VectorMemoryStore.for_user()``.
    Callers that need to override (e.g. tests) should monkeypatch the env
    var and call ``_lightrag_enabled.cache_clear()``.
    """
    requested = _read_request_flag()
    if not requested:
        return False

    try:
        import lightrag  # noqa: F401  # presence check only
    except ImportError:
        _log.warning(
            "OBSCURA_LIGHTRAG requested but lightrag-hku is not installed. "
            "Falling back to vector-only memory. "
            "Install with: uv sync --extra lightrag",
        )
        return False

    return True


def _read_request_flag() -> bool:
    """Return True iff the user requested LightRAG via env or config.

    Separated from ``_lightrag_enabled`` so we can unit-test the precedence
    logic without monkey-patching ``importlib``.
    """
    env = os.environ.get("OBSCURA_LIGHTRAG", "").strip().lower()
    if env in ("on", "1", "true", "yes"):
        return True
    if env in ("off", "0", "false", "no"):
        return False
    # env unset -> consult config.toml
    try:
        from obscura.core.config_io import try_load_config

        cfg = try_load_config(Path.home() / ".obscura" / "config.toml")
        if cfg is None:
            return False
        section = cfg.get("vector_memory", {}).get("lightrag", {})
        return bool(section.get("enabled", False))
    except Exception:
        _log.debug("Could not read LightRAG flag from config.toml", exc_info=True)
        return False
```

Notes:

- `_lightrag_enabled()` is `@lru_cache`d. Phase 2/3 hot paths call it on every `for_user()`; the cache makes it amortized free. Tests must call `_lightrag_enabled.cache_clear()` on setup/teardown if they manipulate the env var.
- The `try_load_config` helper is already the canonical pattern (`obscura/vector_memory/decay.py:200-207` uses it identically).

### 3.3 `/Users/elliottbregni/dev/obscura-main/obscura/lightrag_memory/scoring.py`

Pure math + config reader. Zero IO at module-import time, no `lightrag` import. Fully unit-testable without the extra. This file is what the unit suite leans on most heavily.

```python
"""obscura.lightrag_memory.scoring — Hybrid-score math and weight config.

The hybrid retrieval score combines four signals:

    score = w_v * vector_similarity
          + w_g * graph_relevance
          + w_d * recency_decay_multiplier
          + w_u * usage_frequency_normalized

Weights default to (0.5, 0.3, 0.15, 0.05). They can be overridden in
``~/.obscura/config.toml`` under the ``[vector_memory.lightrag.weights]``
section.

This module has no runtime dependency on the ``lightrag`` package — it is
imported by both the hybrid store (which needs the extra) and by tests
(which don't).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HybridWeights:
    """Weights for the four signals that make up the hybrid score.

    The defaults are calibrated for personal-memory workloads where vector
    similarity remains the dominant signal but graph context contributes
    meaningfully when entities overlap. Tune via config.toml.

    Invariant: all four weights should be in [0, 1]. They are *not* required
    to sum to 1.0 — the score itself is unbounded but in practice falls in
    roughly [0, 1] given normalized inputs.
    """

    vector: float = 0.5
    graph: float = 0.3
    decay: float = 0.15
    usage: float = 0.05

    def validate(self) -> None:
        """Raise ValueError if any weight is out of [0, 1]."""
        for name in ("vector", "graph", "decay", "usage"):
            v = getattr(self, name)
            if not (0.0 <= v <= 1.0):
                msg = f"HybridWeights.{name} must be in [0, 1], got {v!r}"
                raise ValueError(msg)


# Saturate usage at ~100 accesses. log1p(100) ≈ 4.615 — anything beyond that
# contributes diminishing returns. Tunable in a future phase if needed.
_USAGE_SATURATION = 100.0


def hybrid_score(
    *,
    vector_sim: float,
    graph_relevance: float,
    decay_multiplier: float,
    usage_count: int,
    weights: HybridWeights | None = None,
) -> float:
    """Combine the four signals into a single rerank score.

    Parameters
    ----------
    vector_sim:
        Cosine similarity from the vector store, in roughly [0, 1].
    graph_relevance:
        LightRAG's graph-relevance score for the same chunk, in [0, 1].
        Pass ``0.0`` if the chunk did not appear in the graph hits — the
        hybrid score then reduces to a vector + decay + usage blend.
    decay_multiplier:
        Output of :func:`obscura.vector_memory.decay.compute_decay`, in
        ``[0, 1]``. ``1.0`` means no decay; ``0.0`` means fully decayed.
    usage_count:
        Number of times this memory has been accessed. Will be log-scaled
        and saturated at :data:`_USAGE_SATURATION`.
    weights:
        Optional :class:`HybridWeights`. Defaults to the canonical
        ``HybridWeights()``.

    Returns
    -------
    float
        The hybrid score. Higher is more relevant. Not bounded but in
        practice ≤ ``sum(weights)`` for normalized inputs.
    """
    w = weights or HybridWeights()
    usage_norm = math.log1p(max(usage_count, 0)) / math.log1p(_USAGE_SATURATION)
    return (
        w.vector * vector_sim
        + w.graph * graph_relevance
        + w.decay * decay_multiplier
        + w.usage * min(usage_norm, 1.0)
    )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_hybrid_weights(raw: dict[str, Any] | None = None) -> HybridWeights:
    """Build :class:`HybridWeights` from a raw config dict.

    *raw* should be the ``[vector_memory.lightrag.weights]`` section of
    ``config.toml``. Any missing field falls back to the dataclass default.
    Returns the canonical defaults when *raw* is None or empty.
    """
    if not raw:
        return HybridWeights()
    defaults = HybridWeights()
    return HybridWeights(
        vector=float(raw.get("vector", defaults.vector)),
        graph=float(raw.get("graph", defaults.graph)),
        decay=float(raw.get("decay", defaults.decay)),
        usage=float(raw.get("usage", defaults.usage)),
    )


def load_hybrid_weights_from_disk() -> HybridWeights:
    """Load weights from ``~/.obscura/config.toml``.

    Reads ``[vector_memory.lightrag.weights]``. Returns canonical defaults
    if the section is missing or unreadable. Mirrors the contract of
    :func:`obscura.vector_memory.decay.load_decay_config_from_disk`.
    """
    try:
        from obscura.core.config_io import try_load_config

        home_cfg = try_load_config(Path.home() / ".obscura" / "config.toml")
        raw = (
            (home_cfg or {})
            .get("vector_memory", {})
            .get("lightrag", {})
            .get("weights")
        )
        weights = load_hybrid_weights(raw)
        weights.validate()
        return weights
    except Exception:
        logger.debug(
            "Could not load hybrid weights from disk, using defaults",
            exc_info=True,
        )
        return HybridWeights()
```

This file copies the loader pattern from `obscura/vector_memory/decay.py:194-213` deliberately — same shape, same `try_load_config` helper, same exception-swallow-and-log fallback.

### 3.4 `/Users/elliottbregni/dev/obscura-main/obscura/lightrag_memory/adapter.py`

Owns one `LightRAG` instance per user. Bridges sync→async via a single dedicated event-loop thread per adapter. Read §6 for the concurrency rationale.

```python
"""obscura.lightrag_memory.adapter — Per-user LightRAG instance owner.

Bridges Obscura's sync write path to LightRAG's async API by running a
single dedicated event loop in a daemon thread per user. ``insert_safe`` /
``delete_safe`` schedule coroutines onto that loop and return without
waiting; ``aquery`` is async-native for the read path.

This module imports ``lightrag`` at top level. Anything that imports it
must be gated by ``_lightrag_enabled()`` from the package ``__init__``,
or it must be prepared to catch ``ImportError``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    from lightrag import LightRAG, QueryParam
except ImportError as exc:  # pragma: no cover — guarded by _lightrag_enabled
    msg = (
        "obscura.lightrag_memory.adapter requires the 'lightrag' optional "
        "extra. Install with: uv sync --extra lightrag "
        "(or: pip install obscura[lightrag])"
    )
    raise ImportError(msg) from exc

if TYPE_CHECKING:
    from collections.abc import Callable

    from obscura.auth.models import AuthenticatedUser

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GraphHit — placeholder return type for aquery (Phase 3 will populate)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphHit:
    """A single retrieval hit from LightRAG, before Obscura hydration.

    Phase 1 returns an empty list from :meth:`LightRAGAdapter.aquery` —
    Phase 3 will populate this from LightRAG's actual response shape.
    Documented here so the read path's downstream consumers (Phase 3)
    can be type-checked against this contract.
    """

    namespace: str
    key: str
    vector_sim: float
    graph_relevance: float
    text: str
    metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# LightRAGAdapter
# ---------------------------------------------------------------------------


def _user_hash(user_id: str) -> str:
    """16-char hex digest of the user_id, matching ``vector_memory.py:297``."""
    return hashlib.sha256(user_id.encode()).hexdigest()[:16]


def _working_dir(user_id: str) -> Path:
    """Per-user working dir. Lives next to ``~/.obscura/qdrant/``."""
    base = Path(
        os.environ.get(
            "OBSCURA_LIGHTRAG_WORKING_DIR_BASE",
            Path.home() / ".obscura" / "lightrag",
        ),
    )
    return base / _user_hash(user_id)


def _qdrant_collection_name(user_id: str) -> str:
    """LightRAG's Qdrant collection — namespaced separately from the main store.

    Existing store: ``user_<hash>`` (qdrant_backend.py:61).
    LightRAG store: ``obscura_lightrag_<hash>``.

    Keeping them in distinct collections means a botched LightRAG ingest
    can never corrupt the canonical vector memory.
    """
    return f"obscura_lightrag_{_user_hash(user_id)}"


def _qdrant_kwargs() -> dict[str, Any]:
    """Read Qdrant connection details from existing envvars.

    Mirrors the env-reading pattern at ``vector_memory.py:251-254`` so the
    adapter shares whatever Qdrant the user already configured.
    """
    mode = os.environ.get("OBSCURA_QDRANT_MODE", "local").lower()
    if mode == "memory":
        return {"location": ":memory:"}
    if mode == "cloud":
        return {
            "url": os.environ.get("OBSCURA_QDRANT_URL")
            or os.environ.get("QDRANT_URL", "http://localhost:6333"),
            "api_key": os.environ.get("OBSCURA_QDRANT_API_KEY")
            or os.environ.get("QDRANT_API_KEY"),
        }
    # local
    return {
        "path": str(
            Path(
                os.environ.get(
                    "OBSCURA_QDRANT_PATH",
                    Path.home() / ".obscura" / "qdrant",
                ),
            ),
        ),
    }


class LightRAGAdapter:
    """Per-user LightRAG instance + a dedicated event-loop thread.

    Singleton-per-user-id, mirroring :class:`VectorMemoryStore`'s pattern.
    Construction is fail-safe: if anything goes wrong (Qdrant unreachable,
    working_dir read-only, embedding-fn raises) the adapter raises and the
    caller in :func:`_lightrag_enabled` logs + falls back.
    """

    _instances: dict[str, LightRAGAdapter] = {}
    _lock = threading.Lock()

    # Memory types eligible for graph indexing. Phase 2 will read this.
    indexable_types: frozenset[str] = frozenset({"fact", "summary", "general"})

    def __init__(
        self,
        user: AuthenticatedUser,
        embedding_fn: Callable[[str], list[float]],
    ) -> None:
        self.user = user
        self.user_id = user.user_id
        self._embedding_fn = embedding_fn

        # Probe embedding dim once so we can hand it to LightRAG.
        self._embedding_dim = len(embedding_fn("test"))

        self._working_dir = _working_dir(user.user_id)
        self._working_dir.mkdir(parents=True, exist_ok=True)

        self._collection = _qdrant_collection_name(user.user_id)

        # Bring up the dedicated loop BEFORE constructing LightRAG, because
        # LightRAG's __init__ may schedule coroutines.
        self._loop, self._loop_thread = _start_loop_thread(
            name=f"lr-loop-{_user_hash(user.user_id)[:8]}",
        )

        self._lightrag = self._build_lightrag()

    # -- public API ---------------------------------------------------------

    @classmethod
    def for_user(
        cls,
        user: AuthenticatedUser,
        embedding_fn: Callable[[str], list[float]],
    ) -> LightRAGAdapter:
        """Get-or-create the per-user adapter."""
        with cls._lock:
            if user.user_id not in cls._instances:
                cls._instances[user.user_id] = cls(user, embedding_fn)
            return cls._instances[user.user_id]

    @classmethod
    def reset_instances(cls) -> None:
        """Clear singleton cache. For testing only."""
        with cls._lock:
            for adapter in cls._instances.values():
                adapter.shutdown()
            cls._instances.clear()

    def insert_safe(
        self,
        doc_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> Future[Any]:
        """Schedule an async insert onto the dedicated loop and return.

        Phase 1 placeholder: this submits the coroutine and returns the
        ``concurrent.futures.Future``. Phase 2 will wire this from
        ``HybridVectorMemoryStore.set()``. Errors are logged + swallowed
        in the future's done-callback so they never propagate to the
        caller's write path.
        """
        coro = self._ainsert(doc_id, text, metadata or {})
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(self._log_future_error("insert", doc_id))
        return future

    def delete_safe(self, doc_id: str) -> Future[Any]:
        """Schedule an async delete onto the dedicated loop and return."""
        coro = self._adelete(doc_id)
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(self._log_future_error("delete", doc_id))
        return future

    async def aquery(
        self,
        query: str,
        mode: str = "hybrid",
        top_k: int = 20,
    ) -> list[GraphHit]:
        """Run a hybrid retrieval against the LightRAG instance.

        **Phase 1 placeholder.** Returns ``[]`` unconditionally. Phase 3
        will:

        1. Build a :class:`QueryParam` with ``mode`` and ``top_k`` and
           ``only_need_context=True`` to suppress LLM answer synthesis.
        2. Call ``await self._lightrag.aquery(query, param=param)``.
        3. Parse the response into a list of :class:`GraphHit` carrying
           ``obscura_namespace`` / ``obscura_key`` from the metadata
           that Phase 2 stamped into the doc.
        """
        _log.debug(
            "LightRAGAdapter.aquery is a Phase-1 placeholder; returning []. "
            "query=%r mode=%r top_k=%d",
            query[:80],
            mode,
            top_k,
        )
        return []

    def shutdown(self) -> None:
        """Stop the dedicated event loop. Idempotent."""
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=2.0)

    # -- internals ----------------------------------------------------------

    def _build_lightrag(self) -> LightRAG:
        """Construct the LightRAG instance bound to this user's storage.

        Wraps the user-supplied embedding_fn into LightRAG's expected
        ``embedding_func`` shape (``EmbeddingFunc`` with ``func`` async).
        """
        from lightrag.utils import EmbeddingFunc

        async def _async_embed(texts: list[str]) -> list[list[float]]:
            # LightRAG batches; our embedding_fn is per-string. Run on the
            # loop's default executor so we don't block the loop on heavy
            # local sentence-transformer encoding.
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                lambda: [self._embedding_fn(t) for t in texts],
            )

        embedding_func = EmbeddingFunc(
            embedding_dim=self._embedding_dim,
            max_token_size=8192,
            func=_async_embed,
        )

        # Configure LightRAG to use the existing Qdrant.
        # NetworkX graph storage serializes to a pickle in working_dir.
        os.environ.setdefault("QDRANT_URL", _qdrant_kwargs().get("url", ""))
        os.environ.setdefault("QDRANT_API_KEY", _qdrant_kwargs().get("api_key", "") or "")

        return LightRAG(
            working_dir=str(self._working_dir),
            embedding_func=embedding_func,
            vector_storage="QdrantVectorDBStorage",
            graph_storage="NetworkXStorage",
            kv_storage="JsonKVStorage",
            doc_status_storage="JsonDocStatusStorage",
            namespace_prefix=self._collection,
        )

    async def _ainsert(
        self,
        doc_id: str,
        text: str,
        metadata: dict[str, Any],
    ) -> None:
        """Async wrapper around LightRAG's ``ainsert``. Phase 2 will polish."""
        # LightRAG's API is ``ainsert(input, ids=...)`` with ids list-aligned
        # to input list. We pass single-doc lists.
        await self._lightrag.ainsert(
            input=[text],
            ids=[doc_id],
            file_paths=[metadata.get("source", "obscura")],
        )

    async def _adelete(self, doc_id: str) -> None:
        """Async wrapper around LightRAG's delete-by-doc-id."""
        await self._lightrag.adelete_by_doc_id(doc_id)

    @staticmethod
    def _log_future_error(op: str, doc_id: str):
        """Done-callback factory: log + swallow exceptions from the future."""

        def _cb(fut: Future[Any]) -> None:
            try:
                fut.result()
            except Exception:
                _log.warning(
                    "LightRAG %s failed for doc_id=%s; vector store write was "
                    "unaffected.",
                    op,
                    doc_id,
                    exc_info=True,
                )

        return _cb


# ---------------------------------------------------------------------------
# Dedicated-loop helper
# ---------------------------------------------------------------------------


def _start_loop_thread(*, name: str) -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """Spin up a daemon thread running its own asyncio event loop.

    Returns the (loop, thread) pair. The loop is ready to accept coroutines
    via :func:`asyncio.run_coroutine_threadsafe` once this function returns
    (we wait on a one-shot ``threading.Event`` to confirm the loop is up).

    The thread is a daemon so a crash on shutdown won't hang the host.
    """
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        try:
            loop.run_forever()
        finally:
            loop.close()

    thread = threading.Thread(target=_run, name=name, daemon=True)
    thread.start()
    ready.wait(timeout=5.0)
    if not ready.is_set():
        msg = f"asyncio loop thread {name!r} failed to start within 5s"
        raise RuntimeError(msg)
    return loop, thread
```

The `try / except ImportError` at the top of the file is what gives acceptance criterion #2 its controlled error message. **Don't move it inside the class** — module import must fail loudly with a clear message when the extra is missing.

### 3.5 `/Users/elliottbregni/dev/obscura-main/obscura/lightrag_memory/hybrid_store.py`

Empty subclass at this phase. The whole point of this file existing in Phase 1 is to give Phase 2 a stable import path and signature to plug into.

```python
"""obscura.lightrag_memory.hybrid_store — Drop-in subclass of VectorMemoryStore.

Phase 1: empty subclass. The fan-out logic on ``set()`` / ``delete()`` and
the new ``search_hybrid()`` method land in Phases 2-3.

The constructor signature is locked here so callers (and tests) can refer
to ``HybridVectorMemoryStore(user, lightrag_adapter=...)`` from Phase 1
onwards even though the overrides aren't filled in yet.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from obscura.vector_memory import VectorMemoryStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from obscura.auth.models import AuthenticatedUser
    from obscura.lightrag_memory.adapter import LightRAGAdapter
    from obscura.memory.events import EventSink
    from obscura.vector_memory.backends import VectorBackend
    from obscura.vector_memory.decay import DecayConfig

_log = logging.getLogger(__name__)


class HybridVectorMemoryStore(VectorMemoryStore):
    """Vector memory store with LightRAG fan-out.

    Phase 1 stub. Inherits the entire :class:`VectorMemoryStore` API
    unchanged. Phases 2-3 will override:

    - :meth:`set` — fan out to ``self._lr.insert_safe`` after super().set
    - :meth:`delete` — fan out to ``self._lr.delete_safe``
    - :meth:`search_hybrid` — new method, returns rerank-via-graph results
    """

    def __init__(
        self,
        user: AuthenticatedUser,
        *,
        lightrag_adapter: LightRAGAdapter,
        backend: VectorBackend | None = None,
        embedding_fn: Callable[[str], list[float]] | None = None,
        decay_config: DecayConfig | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        super().__init__(
            user,
            backend=backend,
            embedding_fn=embedding_fn,
            decay_config=decay_config,
            event_sink=event_sink,
        )
        self._lr = lightrag_adapter
        self._ingest_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix=f"lr-ingest-{user.user_id[:8]}",
        )
        _log.debug(
            "HybridVectorMemoryStore initialized for user=%s (Phase 1 stub: "
            "no fan-out wiring active yet)",
            user.user_id[:8],
        )
```

The `_ingest_executor` is constructed in Phase 1 even though it's unused. That keeps the constructor stable across phases — Phase 2 just adds the `submit()` calls in `set()`.

### 3.6 `/Users/elliottbregni/dev/obscura-main/obscura/lightrag_memory/ingest.py`

Empty stub. Phase 2 fills it.

```python
"""obscura.lightrag_memory.ingest — Async write-path helpers.

Phase 1 stub. Phase 2 will add helpers that:

- chunk long text before handing it to LightRAG,
- batch concurrent inserts with bounded concurrency,
- consult ``LightRAGAdapter.indexable_types`` to skip non-indexable memory_types.
"""

from __future__ import annotations

# Intentionally empty. See module docstring.
```

### 3.7 `/Users/elliottbregni/dev/obscura-main/obscura/lightrag_memory/backfill.py`

Empty stub. Phase 5 fills it.

```python
"""obscura.lightrag_memory.backfill — Batch migration of existing vector chunks.

Phase 1 stub. Phase 5 will add:

- a CLI subcommand entry (``obscura memory backfill-graph``),
- iteration over ``backend.list_keys`` with rate limiting,
- idempotency via a ``lr_indexed_at`` metadata flag.
"""

from __future__ import annotations

# Intentionally empty. See module docstring.
```

---

## 4. Feature flag wiring

Single source of truth: `obscura.lightrag_memory._lightrag_enabled()` (file 3.2 above).

### Precedence (locked)

1. **Env var** `OBSCURA_LIGHTRAG`:
   - `on` / `1` / `true` / `yes` (case-insensitive) → request enabled.
   - `off` / `0` / `false` / `no` → request disabled, **stops the chain** (config.toml ignored).
   - unset / empty → fall through to (2).
2. **`~/.obscura/config.toml`** under `[vector_memory.lightrag]`:
   ```toml
   [vector_memory.lightrag]
   enabled = true
   ```
3. **Default**: `False`.

### Importability check

Even when (1) or (2) requests enable, `_lightrag_enabled()` must verify `import lightrag` succeeds. If it doesn't, log a one-time warning and return `False`. This makes `OBSCURA_LIGHTRAG=on` safe to set on any machine — never crashes.

### Caching

`@lru_cache(maxsize=1)` at module level. Phase 2/3 hot paths (every `for_user()`) call this. Tests that flip the env var must call `_lightrag_enabled.cache_clear()` themselves.

### Where the flag is checked in this phase

**Nowhere in production code.** Phase 1 ships the flag-check function but does not gate any behavior on it. The first real call site lands in Phase 2 inside `VectorMemoryStore.for_user()`.

This is intentional: it lets us land the scaffolding, run the tests, and ship the PR without changing any caller behavior.

---

## 5. Connection / storage configuration

### Working directory

`~/.obscura/lightrag/<user_hash>/`

- `<user_hash>` = `hashlib.sha256(user.user_id.encode()).hexdigest()[:16]` — **identical** to the convention at `vector_memory.py:297` and `qdrant_backend.py:60`. Sharing the hash function means we can correlate dirs across stores by eye when debugging.
- Override base dir with `OBSCURA_LIGHTRAG_WORKING_DIR_BASE` (mirrors `OBSCURA_QDRANT_PATH`).
- Created with `parents=True, exist_ok=True` in `LightRAGAdapter.__init__`. If the FS is read-only, `mkdir` raises and adapter construction fails — `_lightrag_enabled()` logs and the session falls back. See §7.

### Qdrant collection

`obscura_lightrag_<user_hash>`

- Distinct from the canonical `user_<user_hash>` collection used by `QdrantBackend` (`qdrant_backend.py:61`). Two reasons:
  1. **Schema isolation.** LightRAG writes its own payload shape (entity vectors, relationship vectors). Mixing with Obscura's `VectorEntry` payloads in one collection invites field-name collisions.
  2. **Blast-radius isolation.** A botched LightRAG ingest (corrupt graph state, bad LLM extraction) cannot ever damage the canonical store.

### Qdrant connection

Inherited from existing envvars without duplication:

| Env var | Purpose | Read at |
|---|---|---|
| `OBSCURA_QDRANT_MODE` | `local` / `memory` / `cloud` | `vector_memory.py:251` |
| `OBSCURA_QDRANT_PATH` | local mode dir | `vector_memory.py:252` |
| `OBSCURA_QDRANT_URL` | cloud mode URL | `vector_memory.py:253` |
| `OBSCURA_QDRANT_API_KEY` | cloud mode key | `vector_memory.py:254` |

`adapter._qdrant_kwargs()` reads these once. LightRAG's `QdrantVectorDBStorage` reads `QDRANT_URL` / `QDRANT_API_KEY` from the env, so the adapter sets them via `os.environ.setdefault` before constructing `LightRAG()`. If they're already set, the adapter respects them.

### Embedding function

Shared with `VectorMemoryStore` via `_make_default_embedding_fn` (`vector_memory.py:86`). The adapter receives the embedder as a parameter — it does **not** call `_make_default_embedding_fn` itself. That keeps test injection clean and avoids loading sentence-transformers twice.

LightRAG expects an `EmbeddingFunc` whose `func` is a coroutine returning `list[list[float]]`. We wrap the per-string sync `embedding_fn` accordingly (file 3.4 above, `_async_embed`). The wrapper offloads to the loop's default executor so heavy local encodes don't block the loop.

### Graph backend

`graph_storage="NetworkXStorage"`. NetworkX serializes its pickle to `<working_dir>/graph_chunk_entity_relation.pkl` (LightRAG's default). No additional config needed. As noted in `00-overview.md`, swap to AGE later if/when multi-tenant.

---

## 6. Concurrency model

### Problem

`VectorMemoryStore.set()` is sync. Existing callers (`memory_tools.py:179`, `routes/vector_memory.py:41`, etc.) are sync. LightRAG's API is async. Wrapping each call in `asyncio.run()` would:

1. Spin up a fresh loop per call (~ms-scale overhead, but adds up under load).
2. Crash if the caller is itself inside an asyncio context (existing async code paths).

### Solution

**One dedicated daemon-thread event loop per `LightRAGAdapter` instance.** Constructed in `__init__`, kept alive for the life of the adapter, torn down in `shutdown()` (or implicit at process exit since the thread is a daemon).

Sync→async bridge: `asyncio.run_coroutine_threadsafe(coro, self._loop)` — returns a `concurrent.futures.Future` immediately; the coroutine runs on the dedicated loop's thread.

**Why not a `ThreadPoolExecutor.submit(asyncio.run, coro)`?** That spawns a fresh loop per call.

**Why not the global `asyncio.get_event_loop()`?** That couples adapter lifecycle to the caller's event-loop policy. In a multi-process REST server with a per-request loop, the adapter would be unusable.

**Why one loop per adapter rather than one global loop?** The adapter is a per-user singleton, so one-loop-per-user matches its scope. Avoids cross-user blocking on shared loop saturation. Cost is one extra thread per active user — negligible at single-user-per-machine workloads.

### Boilerplate (already in file 3.4)

```python
def _start_loop_thread(*, name):
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        try:
            loop.run_forever()
        finally:
            loop.close()

    thread = threading.Thread(target=_run, name=name, daemon=True)
    thread.start()
    ready.wait(timeout=5.0)
    return loop, thread
```

The `ready` event is non-negotiable. Without it, the very first `run_coroutine_threadsafe` after `__init__` returns can race the loop's startup and silently no-op.

### Phase 2 layering on top

Phase 2 adds a `ThreadPoolExecutor` (`_ingest_executor`) inside `HybridVectorMemoryStore`. Its submitted callable is `self._lr.insert_safe(...)` — which itself does `run_coroutine_threadsafe`. Two threads of indirection sounds excessive but each layer pulls weight:

- **`_ingest_executor`** decouples the *write site* from any waiting. `set()` returns instantly.
- **`_loop_thread`** decouples *all async work* from any per-call event-loop construction.

The submit chain looks like:

```
caller (sync) → set() (sync) → submit() → executor worker (sync) →
  insert_safe (sync) → run_coroutine_threadsafe → adapter loop thread (async)
```

Strict ordering of these layers matters: the executor exists so we can rate-limit the submission rate (Phase 5 adds a semaphore to back-pressure backfill); the loop thread exists because `lightrag` is async-native.

---

## 7. Failure modes

### `lightrag` not installed but `OBSCURA_LIGHTRAG=on`

Caught by `_lightrag_enabled()` (file 3.2): `try: import lightrag` fails → log warning once → return `False`. **No crash.** Caller transparently uses the vanilla `VectorMemoryStore`.

### Qdrant unreachable when `LightRAG()` is constructed

`LightRAGAdapter.__init__` calls `LightRAG(...)` which initializes `QdrantVectorDBStorage`. If Qdrant is down, that raises. Phase 2's call site (inside `VectorMemoryStore.for_user`) must wrap adapter construction in `try / except` and fall back to vanilla on failure:

```python
# Phase 2 wiring (NOT in this phase)
try:
    adapter = LightRAGAdapter.for_user(user, embedding_fn)
    return HybridVectorMemoryStore(user, lightrag_adapter=adapter, ...)
except Exception as exc:
    _log.warning("LightRAG adapter init failed; falling back to vector-only: %s", exc)
    return cls(user, embedding_fn=embedding_fn)
```

Phase 1 doesn't ship this wrapper because Phase 1 doesn't ship the call site. But the design must accommodate it — the adapter raising on Qdrant failure is the *correct* behavior; it gives the caller a clean signal to fall back.

### `insert_safe` raises after the canonical write succeeded

The vector store write already happened in `super().set()`. The fan-out is best-effort. The `_log_future_error` done-callback (file 3.4) logs at WARN level and swallows. **Never propagated to the caller.**

This is the load-bearing invariant of the entire integration: **the canonical vector store is the source of truth.** LightRAG provides graph-aware retrieval over that truth; if it falls behind or fails, retrieval degrades (Phase 3's fallback to `super().search_reranked()`) but no data is ever lost.

### `working_dir` on read-only filesystem

`Path(...).mkdir(parents=True, exist_ok=True)` raises `PermissionError`. `LightRAGAdapter.__init__` does not catch this — it propagates. Phase 2's call-site try/except (above) catches and falls back. The relevant log line is in the try/except wrapper, not in the adapter.

### Loop thread fails to start within 5s

`_start_loop_thread` raises `RuntimeError` after the timeout. Same handling as Qdrant failure: propagates out of `__init__`, Phase 2 wrapper catches, falls back to vanilla. Vanishingly rare in practice but cheap to guard.

### Embedding fn raises during `__init__` dim probe

`embedding_fn("test")` is called once in `__init__` to determine `_embedding_dim`. If the user's embedder is broken, this raises. Same fallback chain.

---

## 8. Tests for this phase

Five test files. Total: ~80 lines of test code. Phase 6 carries the bulk of the testing — Phase 1 just confirms scaffold integrity.

**Location:** `/Users/elliottbregni/dev/obscura-main/tests/unit/obscura/lightrag_memory/`

### `tests/unit/obscura/lightrag_memory/__init__.py`

Empty file. Required for pytest discovery.

### `tests/unit/obscura/lightrag_memory/test_scoring.py`

```python
"""Phase 1 — scoring math tests. Must work without ``lightrag`` installed."""

from __future__ import annotations

import math

import pytest

from obscura.lightrag_memory.scoring import (
    HybridWeights,
    hybrid_score,
    load_hybrid_weights,
)


class TestHybridWeights:
    def test_defaults(self) -> None:
        w = HybridWeights()
        assert w.vector == 0.5
        assert w.graph == 0.3
        assert w.decay == 0.15
        assert w.usage == 0.05

    def test_validate_passes_for_defaults(self) -> None:
        HybridWeights().validate()  # no raise

    def test_validate_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="vector"):
            HybridWeights(vector=1.5).validate()


class TestHybridScore:
    def test_all_max_inputs_equals_weight_sum(self) -> None:
        # vector=1, graph=1, decay=1, usage_count saturated → score == sum(weights)
        score = hybrid_score(
            vector_sim=1.0,
            graph_relevance=1.0,
            decay_multiplier=1.0,
            usage_count=10_000,  # saturates above 100
            weights=HybridWeights(),
        )
        assert score == pytest.approx(0.5 + 0.3 + 0.15 + 0.05)

    def test_all_zero_inputs_equals_zero(self) -> None:
        score = hybrid_score(
            vector_sim=0.0,
            graph_relevance=0.0,
            decay_multiplier=0.0,
            usage_count=0,
        )
        assert score == 0.0

    def test_negative_usage_treated_as_zero(self) -> None:
        score_zero = hybrid_score(
            vector_sim=0, graph_relevance=0, decay_multiplier=0, usage_count=0,
        )
        score_neg = hybrid_score(
            vector_sim=0, graph_relevance=0, decay_multiplier=0, usage_count=-5,
        )
        assert score_zero == score_neg


class TestLoadHybridWeights:
    def test_empty_dict_returns_defaults(self) -> None:
        assert load_hybrid_weights({}) == HybridWeights()

    def test_partial_override(self) -> None:
        w = load_hybrid_weights({"graph": 0.6})
        assert w.graph == 0.6
        # Unmentioned fields keep defaults
        assert w.vector == 0.5
```

### `tests/unit/obscura/lightrag_memory/test_feature_flag.py`

```python
"""Phase 1 — feature flag precedence tests."""

from __future__ import annotations

import pytest

import obscura.lightrag_memory as lr


@pytest.fixture(autouse=True)
def _clear_flag_cache(monkeypatch):
    monkeypatch.delenv("OBSCURA_LIGHTRAG", raising=False)
    lr._lightrag_enabled.cache_clear()
    yield
    lr._lightrag_enabled.cache_clear()


def test_default_is_false() -> None:
    assert lr._lightrag_enabled() is False


def test_env_off_explicit(monkeypatch) -> None:
    monkeypatch.setenv("OBSCURA_LIGHTRAG", "off")
    assert lr._lightrag_enabled() is False


def test_env_on_without_extra_falls_back_to_false(monkeypatch) -> None:
    """OBSCURA_LIGHTRAG=on but lightrag not installed → False with warning."""
    monkeypatch.setenv("OBSCURA_LIGHTRAG", "on")
    # Force the import probe to fail even if the extra is somehow installed
    # in the test env. The point is to exercise the fallback path.
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "lightrag":
            raise ImportError("forced-missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    lr._lightrag_enabled.cache_clear()
    assert lr._lightrag_enabled() is False
```

### `tests/unit/obscura/lightrag_memory/test_adapter_import.py`

```python
"""Phase 1 — adapter import surface.

When ``lightrag`` is installed: importing the adapter module is fine.
When ``lightrag`` is NOT installed: the import must raise an ImportError
with a message that names the optional extra. We force the missing-import
case here so the test is robust regardless of dev environment.
"""

from __future__ import annotations

import importlib
import sys

import pytest


def test_adapter_import_raises_controlled_error_when_lightrag_missing(
    monkeypatch,
) -> None:
    # If a previous test imported the adapter module, drop it.
    sys.modules.pop("obscura.lightrag_memory.adapter", None)

    # Pretend lightrag is missing even if it's installed in the dev env.
    monkeypatch.setitem(sys.modules, "lightrag", None)

    with pytest.raises(ImportError, match="lightrag"):
        importlib.import_module("obscura.lightrag_memory.adapter")
```

`monkeypatch.setitem(sys.modules, "lightrag", None)` is the canonical pytest pattern for forcing `import lightrag` to raise `ImportError` even on a machine where it's installed. This makes the test pass identically in dev/CI/with-extra/without-extra.

### Test runtime check

```bash
pytest tests/unit/obscura/lightrag_memory/ -v
```

Should report 4 tests collected, 4 passed, no warnings (the warning from `_lightrag_enabled` is a logger.warning, not a `warnings.warn`).

---

## 9. Rollback

This phase is fully reversible.

1. `git revert <phase-1-commit-sha>` — undoes:
   - the `pyproject.toml` extra,
   - the `obscura/lightrag_memory/` package,
   - the new test directory.
2. `rm -rf ~/.obscura/lightrag/` — removes any per-user working dirs that were created by exercising the adapter (only created if a developer manually instantiated `LightRAGAdapter`; a clean test run never touches it).
3. (Optional, only if a developer ran the adapter against real Qdrant) `qdrant-cli` or the Qdrant HTTP API: drop any collections matching `obscura_lightrag_*`. Phase 1 doesn't auto-create these on import — only on first `LightRAGAdapter(...)` call — so this is unlikely to be needed.

**No schema migrations.** Phase 1 does not alter the existing Qdrant `user_*` collections, the SQLite vector backend, or any field on `VectorEntry`. There is nothing to roll back at the data layer.

**No public API change.** `VectorMemoryStore.for_user()` returns the same type it did before. Nothing imports `obscura.lightrag_memory` from production code.

---

## 10. Open questions / decisions deferred

Each of these is resolved in a later phase and should be re-checked against this list before that phase ships.

1. **Real `aquery` shape.** Phase 1 returns `[]` from `LightRAGAdapter.aquery`. **Phase 3** must populate `GraphHit` from LightRAG's actual response (which is a string + sources structure when `only_need_context=True`; need to verify against `lightrag-hku>=1.4`).
2. **`obscura_key` / `obscura_namespace` carrier.** The plan in `00-overview.md` §Phase 2 says to embed these in `metadata` so the read path can hydrate by key. Phase 1's `_ainsert` accepts `metadata` but doesn't yet thread it into the LightRAG `ainsert` call (LightRAG 1.4's `ainsert` signature should be re-verified for whether per-doc metadata is supported, or if we have to stash it in a sidecar JSON in `working_dir`).
3. **`indexable_types` configurability.** Phase 1 hardcodes `frozenset({"fact", "summary", "general"})` as a class attribute. **Phase 2** should make this configurable via `[vector_memory.lightrag] indexable_types = ["fact", "summary", "general"]`.
4. **Per-call timeout budget.** `00-overview.md` §Risks mentions `OBSCURA_LIGHTRAG_TIMEOUT_MS=400`. **Phase 3** wires this on the read path; Phase 1 does not.
5. **Shadow-mode telemetry.** `OBSCURA_LIGHTRAG_SHADOW=1` for A/B comparison. **Phase 3.**
6. **Wiring into `for_user()`.** The single integration point at `vector_memory.py:306`. **Phase 2** lands this — Phase 1 deliberately does not.
7. **Consolidation hook for graph-deletes.** When `MemoryConsolidator` deletes consolidated episodes, dangling graph references appear. Hook in `consolidator.consolidate()` ~line 130. **Phase 2** when `delete()` override lands.
8. **Auth middleware.** `obscura/auth/middleware.py:58` calls `for_user()` on first login. With the Phase 2 wiring, that automatically gives every authenticated user a `HybridVectorMemoryStore` if the flag is on. No new code in middleware. Re-verify in Phase 2.
9. **`lightrag-hku` version pin floor.** Set to `>=1.4` based on the `00-overview.md` plan. Re-verify the actual minimum version that ships `QdrantVectorDBStorage` and the `EmbeddingFunc` shape used here before merging.
10. **`access_count` on `VectorEntry`.** Phase 3 adds this field to the Qdrant + SQLite payloads. Phase 1's `hybrid_score()` accepts a `usage_count: int` already, but no current code path provides a non-zero value. That's by design — Phase 1's score reduces to a vector + graph + decay blend until Phase 3 wires usage tracking.

---

## Quick checklist for the implementing engineer

- [ ] Edit `pyproject.toml` — add `lightrag` extra, append to `full`, add to `[tool.setuptools] packages`.
- [ ] Create `obscura/lightrag_memory/__init__.py` (file 3.2).
- [ ] Create `obscura/lightrag_memory/scoring.py` (file 3.3).
- [ ] Create `obscura/lightrag_memory/adapter.py` (file 3.4).
- [ ] Create `obscura/lightrag_memory/hybrid_store.py` (file 3.5).
- [ ] Create `obscura/lightrag_memory/ingest.py` (file 3.6).
- [ ] Create `obscura/lightrag_memory/backfill.py` (file 3.7).
- [ ] Create `tests/unit/obscura/lightrag_memory/__init__.py` (empty).
- [ ] Create `tests/unit/obscura/lightrag_memory/test_scoring.py`.
- [ ] Create `tests/unit/obscura/lightrag_memory/test_feature_flag.py`.
- [ ] Create `tests/unit/obscura/lightrag_memory/test_adapter_import.py`.
- [ ] Run `uv sync --extra lightrag` — confirm clean install.
- [ ] Run `pytest tests/unit/obscura/lightrag_memory/ -v` — 4 passed.
- [ ] Run `pytest tests/ -m "not e2e"` — same pass count as `main`.
- [ ] Run `ruff check obscura/lightrag_memory/ tests/unit/obscura/lightrag_memory/` — clean.
- [ ] Run `pyright obscura/lightrag_memory/` — clean.
- [ ] Confirm `grep -r "lightrag_memory" obscura/` matches only inside `obscura/lightrag_memory/`.
- [ ] Open PR titled `Phase 1: LightRAG dependency + scaffold` referencing this doc.
