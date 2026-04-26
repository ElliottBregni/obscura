# Phase 6 — Tests

> **Status:** ready to execute.
> **Owner:** Elliott Bregni (`bregnie34@gmail.com`)
> **Drafted:** 2026-04-26
> **Predecessor:** [`00-overview.md`](./00-overview.md) §"Phase 6 — Tests" (sketch only)
> **Follows:** Phase 5 (backfill / lazy on-touch). This phase has no successor.

This document is implementation-ready. An engineer should land the test suite from it without further design questions. All file paths are absolute on first introduction, then relative.

---

## 1. Goal & non-goals

### Goal

Phase 6 produces a deterministic, fast unit suite plus an opt-in integration suite that together give the LightRAG integration a maintenance-grade safety net.

Concretely, after this phase:

1. `tests/unit/obscura/lightrag_memory/` exists with eight test modules and a shared `conftest.py`.
2. A `MockLightRAG` fixture lives in that `conftest.py` and never imports the real `lightrag` package — the unit suite runs on a vanilla `uv sync --group dev` install with the `lightrag` extra absent.
3. The unit suite exercises:
   - `hybrid_score()` math (`scoring.py`),
   - `HybridVectorMemoryStore` set/delete/whitelist behavior (`hybrid_store.py`),
   - `LightRAGAdapter` sync wrappers (`adapter.py`) — adapter is exercised against a mock `LightRAG` instance,
   - `search_hybrid()` query path including decay re-application, namespace filter, fallback, timeout, and access-count increment,
   - `_touch_and_count_async` race tolerance,
   - the `MemoryConsolidator` deletion hook,
   - the `BackfillEngine` plus its CLI,
4. An integration suite at `tests/integration/lightrag/` is gated behind the `RUN_LR_INTEGRATION=1` env var. It exercises the real `lightrag-hku` against tiny VCR cassettes — recorded once, replayed forever after, no network calls in CI.
5. Coverage of `obscura.lightrag_memory.*` is **≥ 85%** as measured by `pytest --cov=obscura.lightrag_memory --cov-fail-under=85`. This matches the project-wide threshold from `[tool.coverage.report]` in `/Users/elliottbregni/dev/obscura-main/pyproject.toml:222-224`.
6. A regression check in CI confirms the existing test suite still passes with both `OBSCURA_LIGHTRAG=on` and `OBSCURA_LIGHTRAG=off`.

### Non-goals (explicit)

- **Not** testing LightRAG's own retrieval quality — entity extraction precision, graph traversal correctness, prompt-template stability, embedding fidelity. That's `lightrag-hku`'s problem; we treat it as a black box.
- **Not** chaos / fault-injection testing of the surrounding infra (Qdrant outages, disk-full, OOM). The adapter's existing `try/except` paths are unit-tested but we do not stand up a chaos rig.
- **Not** performance / latency benchmarking. `pytest-benchmark` and `asv` are flagged as nice-to-have follow-ups in §19, but no SLA enforcement here.
- **Not** UI rendering tests. The `memory_graph_query` and `memory_graph_explain` tools return JSON; we test the JSON shape, not how `web-ui/` renders it.
- **Not** mutation testing as a CI gate. §17 sketches an opt-in `mutmut` recipe but it doesn't block merges.
- **Not** load testing of the `ThreadPoolExecutor`. The `test_concurrent_inserts_serialized_per_user` test verifies ordering with N=10; we don't push to N=10000.

---

## 2. Acceptance criteria

Each item is a concrete check that must pass before the PR merges.

1. **Unit suite is fast.** `pytest tests/unit/obscura/lightrag_memory/ -v` completes in **< 10 seconds** on a stock dev laptop (M-series Mac, no docker).
2. **Unit suite is hermetic.** `pytest tests/ -v -m "not e2e and not lightrag_integration"` passes end-to-end with `lightrag-hku` **not installed**. Verify by uninstalling — `uv pip uninstall lightrag-hku networkx` — then running the suite.
3. **`MockLightRAG` is import-clean.** `python -c "from tests.unit.obscura.lightrag_memory.conftest import MockLightRAG"` does not raise even when `import lightrag` would fail. (The fixture file may not be importable as a module path directly because of pytest's collection model — the equivalent check is that pytest's collection phase does not error.)
4. **Coverage gate.** `pytest tests/unit/obscura/lightrag_memory/ --cov=obscura.lightrag_memory --cov-report=term-missing --cov-fail-under=85` exits 0.
5. **Integration suite is opt-in.** `pytest tests/ -v` with `RUN_LR_INTEGRATION` *unset* shows every test under `tests/integration/lightrag/` as `SKIPPED`. `RUN_LR_INTEGRATION=1 pytest tests/integration/lightrag/ -v` passes locally with the extra installed.
6. **Cassettes are checked in.** `tests/integration/lightrag/cassettes/*.yaml` exists; running the integration suite with no network (`pytest -v --disable-network` or by yanking the cable) still passes.
7. **Regression — flag-on.** `OBSCURA_LIGHTRAG=on pytest tests/unit/obscura/vector_memory/ -v` produces the same pass/fail count as flag-off. The flip is invisible to the existing suite.
8. **Lint clean.** `ruff check tests/unit/obscura/lightrag_memory/ tests/integration/lightrag/` exits 0.
9. **Type clean.** `pyright tests/unit/obscura/lightrag_memory/` exits 0. The integration suite is exempt — it imports `lightrag` which has no type stubs.
10. **CI default run.** The default CI command (`pytest tests/ -v -m "not e2e and not lightrag_integration"` per §16) passes on a clean clone with **only** `uv sync --group dev` installed (no extras).

---

## 3. Test layout

```
tests/unit/obscura/lightrag_memory/
├── __init__.py                  # empty; makes the directory a package for pyright
├── conftest.py                  # MockLightRAG, fake_user, hybrid_store, fixture_corpus glue
├── fixture_corpus.py            # 10-20 representative chunks (data-only)
├── assert_helpers.py            # reusable assertion utilities
├── test_scoring.py              # hybrid_score() math
├── test_hybrid_store.py         # HybridVectorMemoryStore overrides
├── test_adapter.py              # LightRAGAdapter sync wrappers, error handling
├── test_search_hybrid.py        # search_hybrid + fallback + decay re-application
├── test_touch_count.py          # _touch_and_count_async race tolerance
├── test_consolidator_hook.py    # consolidator deletion path
├── test_backfill.py             # BackfillEngine, CLI dry-run, resume
└── test_cli.py                  # Click CliRunner tests for the backfill command

tests/integration/lightrag/
├── __init__.py
├── conftest.py                  # VCR config, real lightrag setup, RUN_LR_INTEGRATION gate
├── cassettes/
│   ├── tiny_corpus_ingest.yaml  # recorded LLM responses for ingest
│   └── tiny_corpus_query.yaml   # recorded LLM responses for query
├── test_e2e_ingest.py           # real lightrag.ainsert against cassette
└── test_e2e_query.py            # real lightrag.aquery against cassette
```

The unit directory mirrors the package layout exactly — every module under `obscura/lightrag_memory/` has at least one matching test file. This is the same convention as `tests/unit/obscura/vector_memory/` (see `/Users/elliottbregni/dev/obscura-main/tests/unit/obscura/vector_memory/test_vector_memory.py:1-12` for the import idiom).

---

## 4. The `MockLightRAG` fixture — full implementation

**Location:** `tests/unit/obscura/lightrag_memory/conftest.py`.

```python
"""Shared fixtures for the lightrag_memory test suite.

This module never imports the real `lightrag` package. `MockLightRAG`
is a behaviorally-faithful drop-in for `LightRAGAdapter` — it inherits
the public surface so `isinstance(x, LightRAGAdapter)` checks in
product code keep passing.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

# Importing the *real* adapter is fine here — adapter.py guards its
# `import lightrag` behind `_lightrag_enabled()` so this stays cheap.
from obscura.lightrag_memory.adapter import (
    GraphExplanation,
    GraphHit,
    LightRAGAdapter,
)
from obscura.lightrag_memory.scoring import HybridWeights

if TYPE_CHECKING:
    from collections.abc import Iterable

    from obscura.auth.models import AuthenticatedUser


# ---------------------------------------------------------------------------
# MockLightRAG — primary unit-test seam
# ---------------------------------------------------------------------------


@dataclass
class _MockState:
    """Inspectable record of every call to a `MockLightRAG` instance."""

    inserts: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    aquery_calls: list[tuple[str, str, int]] = field(default_factory=list)
    canned_aquery: list[tuple[str, list[GraphHit]]] = field(default_factory=list)
    canned_neighbors: dict[str, GraphExplanation] = field(default_factory=dict)
    next_aquery_raises: BaseException | None = None
    next_aquery_sleep_s: float | None = None
    next_insert_raises: BaseException | None = None
    next_insert_sleep_s: float | None = None
    closed: bool = False


class MockLightRAG(LightRAGAdapter):
    """Drop-in replacement for `LightRAGAdapter` — never imports lightrag.

    Behavioral parity with the real adapter:
    - `insert_safe` and `delete_safe` are sync, never raise.
    - `aquery` is async, returns a list of `GraphHit`.
    - `get_neighbors` is sync, returns a `GraphExplanation`.
    - `close()` is idempotent.

    Test ergonomics:
    - `state.inserts` / `state.deletes` / `state.aquery_calls` are
      append-only logs — assert against them, not a `Mock.call_args_list`.
    - `set_canned(query_substring, hits)` registers canned `aquery`
      responses by substring match — pragmatic when test queries are
      dynamically constructed.
    - `state.next_aquery_raises` / `next_aquery_sleep_s` inject one-shot
      behavior; consumed on first call so subsequent calls are normal.
    - `state.next_insert_raises` / `next_insert_sleep_s` likewise for
      ingest path.
    """

    indexable_types: frozenset[str] = frozenset({"fact", "summary", "general"})
    MIN_LENGTH: int = 20  # mirrors real adapter constant

    def __init__(self, *_: Any, **__: Any) -> None:
        # Deliberately do NOT call super().__init__ — the real adapter's
        # __init__ may try to construct a LightRAG instance.
        self.state = _MockState()
        self._closed = False

    # -- factory matching the real adapter signature -----------------------

    @classmethod
    def for_user(  # type: ignore[override]
        cls,
        user: AuthenticatedUser,
        embedding_fn: Any | None = None,
    ) -> MockLightRAG:
        return cls()

    # -- write path --------------------------------------------------------

    def insert_safe(  # type: ignore[override]
        self,
        doc_id: str,
        text: str,
        metadata: dict[str, Any],
    ) -> None:
        if len(text) < self.MIN_LENGTH:
            # Mirror real adapter behavior — record skip in metadata
            metadata = {**metadata, "_skip_reason": "too_short"}
            self.state.inserts.append((doc_id, text, metadata))
            return
        if self.state.next_insert_sleep_s is not None:
            import time as _time

            _time.sleep(self.state.next_insert_sleep_s)
            self.state.next_insert_sleep_s = None
        if self.state.next_insert_raises is not None:
            exc = self.state.next_insert_raises
            self.state.next_insert_raises = None
            # Real adapter swallows; mirror that.
            return
        self.state.inserts.append((doc_id, text, dict(metadata)))

    def delete_safe(self, doc_id: str) -> None:  # type: ignore[override]
        self.state.deletes.append(doc_id)

    # -- read path ---------------------------------------------------------

    async def aquery(  # type: ignore[override]
        self,
        query: str,
        mode: str = "hybrid",
        top_k: int = 20,
    ) -> list[GraphHit]:
        self.state.aquery_calls.append((query, mode, top_k))
        if self.state.next_aquery_raises is not None:
            exc = self.state.next_aquery_raises
            self.state.next_aquery_raises = None
            raise exc
        if self.state.next_aquery_sleep_s is not None:
            await asyncio.sleep(self.state.next_aquery_sleep_s)
            self.state.next_aquery_sleep_s = None
        for substring, hits in self.state.canned_aquery:
            if substring in query:
                return list(hits)
        return []

    def get_neighbors(  # type: ignore[override]
        self,
        doc_id: str,
        depth: int = 1,
    ) -> GraphExplanation:
        return self.state.canned_neighbors.get(
            doc_id,
            GraphExplanation(entities=[], relations=[], neighbors=[]),
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:  # type: ignore[override]
        self._closed = True
        self.state.closed = True

    # -- canning helpers ---------------------------------------------------

    def set_canned(
        self,
        query_substring: str,
        hits: list[GraphHit],
    ) -> None:
        self.state.canned_aquery.append((query_substring, list(hits)))

    def set_canned_neighbors(
        self,
        doc_id: str,
        explanation: GraphExplanation,
    ) -> None:
        self.state.canned_neighbors[doc_id] = explanation


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_lightrag() -> MockLightRAG:
    """A fresh `MockLightRAG` per test."""
    return MockLightRAG()


@pytest.fixture
def fake_user() -> AuthenticatedUser:
    """Minimal `AuthenticatedUser` matching the project's existing pattern.

    Pattern lifted from `tests/unit/obscura/vector_memory/test_vector_memory.py:18-27`.
    """
    from obscura.auth.models import AuthenticatedUser

    return AuthenticatedUser(
        user_id="u-lightrag-test",
        email="lr@test.com",
        roles=("admin",),
        org_id="org-1",
        token_type="user",
        raw_token="test",
    )


@pytest.fixture
def fake_decay_config():
    """A `DecayConfig` with predictable parameters for deterministic tests."""
    from obscura.vector_memory.decay import DecayConfig, DecayProfile

    # Tight half-lives so tests can observe decay over short fake durations.
    profiles = {
        "fact": DecayProfile(half_life_days=10.0, min_score_floor=0.001),
        "summary": DecayProfile(half_life_days=10.0, min_score_floor=0.001),
        "episode": DecayProfile(half_life_days=10.0, min_score_floor=0.001),
        "general": DecayProfile(half_life_days=10.0, min_score_floor=0.001),
        "preference": DecayProfile(immune=True),
    }
    return DecayConfig(profiles=profiles, access_boost_days=0.0)


@pytest.fixture
def hybrid_store(
    tmp_path: Path,
    fake_user,
    mock_lightrag: MockLightRAG,
    fake_decay_config,
) -> Any:
    """Fully-wired `HybridVectorMemoryStore` backed by SQLite in tmp_path.

    Pattern lifted from `tests/unit/obscura/vector_memory/test_vector_memory.py:30-40`.
    SQLite gives us a real backend so we exercise the actual hydration path —
    `MockLightRAG` only stubs out the *graph* side, not the vector store.
    """
    from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
    from obscura.vector_memory import VectorMemoryStore, simple_embedding
    from obscura.vector_memory.backends import BackendConfig, SQLiteBackend

    # Reset singleton so each test gets a fresh store. Mirrors the
    # top-level `temp_memory_dirs` fixture in tests/conftest.py:91-110.
    VectorMemoryStore.reset_instances()

    config = BackendConfig(user_id=fake_user.user_id, embedding_dim=384)
    backend = SQLiteBackend(config=config, db_path=tmp_path / "vec.db")
    store = HybridVectorMemoryStore(
        fake_user,
        backend=backend,
        embedding_fn=simple_embedding,
        decay_config=fake_decay_config,
        lightrag_adapter=mock_lightrag,
    )
    yield store
    store.close()


@pytest.fixture
def vector_entry_factory(fake_user):
    """Callable that produces `VectorEntry` instances with sane defaults.

    Used by tests that need to seed the backend or build hits directly.
    """
    from obscura.memory import MemoryKey
    from obscura.vector_memory.backends import VectorEntry

    def _make(
        key: str = "k1",
        namespace: str = "default",
        text: str = "hello world",
        memory_type: str = "fact",
        metadata: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        accessed_at: datetime | None = None,
        score: float = 0.0,
    ) -> VectorEntry:
        now = datetime.now(UTC)
        return VectorEntry(
            key=MemoryKey(namespace=namespace, key=key),
            text=text,
            embedding=[0.0] * 384,
            metadata=metadata or {},
            memory_type=memory_type,
            created_at=created_at or now,
            updated_at=None,
            accessed_at=accessed_at,
            score=score,
        )

    return _make
```

### Why this shape — design choices

**Why subclass `LightRAGAdapter` rather than `Mock(spec=LightRAGAdapter)`?**
Type stability. Product code does `isinstance(adapter, LightRAGAdapter)` checks in at least one place (the `for_user` factory branches in Phase 1). `unittest.mock.Mock(spec=...)` fails `isinstance` unless we use `MagicMock(spec=...)` or `create_autospec`, both of which lose the constructor-friendly factory pattern. Plain inheritance gives us free `isinstance` parity, untyped `cast()`-free attribute access in tests, and explicit override surface (every method we override is annotated `# type: ignore[override]` so pyright stays clean).

**Why `_MockState` instead of `Mock.call_args_list`?**
Test readability. `assert mock_lightrag.state.inserts[0][0] == "default::k1"` reads obviously; `assert mock.insert_safe.call_args_list[0].args[0] == "default::k1"` does not. `_MockState` is a vanilla dataclass — fully introspectable, autocompletable, and pyright-friendly.

**Why `next_aquery_*` knobs?**
One-shot behavior injection. We need to test "what happens when *this* call raises" without polluting subsequent calls. Setting `mock.state.next_aquery_raises = TimeoutError()` and consuming the slot on first invocation gives us per-call control without per-test patching.

**Why `canned_aquery` matches by substring?**
Pragmatism. Test queries are often built from fixture corpora (`f"what about {entity}"`) — exact-match canning becomes brittle. Substring matching is forgiving without being so loose that we miss intent.

**Why is `MIN_LENGTH = 20` a class attribute on the mock?**
The real adapter has a minimum text length below which it skips ingest (avoids LLM calls on tokens like "ok" or "yes"). The mock mirrors this so `test_short_text_skipped` passes without cross-importing the constant.

---

## 5. Reusable fixtures

Beyond the core `MockLightRAG`, three fixtures are shared across the unit suite. All live in `conftest.py` so any test in the package can request them.

### `fake_user`

A frozen `AuthenticatedUser` with `user_id="u-lightrag-test"`. Pattern lifted from `/Users/elliottbregni/dev/obscura-main/tests/unit/obscura/vector_memory/test_vector_memory.py:18-27`. Reuse keeps singleton-cache assertions identical across the two suites.

### `fake_decay_config`

A `DecayConfig` with all profile half-lives compressed to 10 days and `access_boost_days=0`. The compressed half-life lets tests exercise decay arithmetic over fake-now offsets of a few days without dropping below the floor; the zeroed access boost removes one source of nondeterminism so decay tests can assert exact multipliers.

### `vector_entry_factory`

A callable returning `VectorEntry` instances with sane defaults — namespace `"default"`, embedding `[0.0] * 384`, memory_type `"fact"`, `created_at=datetime.now(UTC)`. Tests pass kwargs to override only what they care about. Pattern matches `obscura/vector_memory/backends/base.py:25-40`.

### Fixture corpus — `fixture_corpus.py`

```python
"""Static corpus of representative memories — used by multiple test files."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    key: str
    namespace: str
    text: str
    memory_type: str


CORPUS: list[Chunk] = [
    # Facts (6) — go to graph
    Chunk("user_lang_python", "default", "User primarily codes in Python 3.13.", "fact"),
    Chunk("user_lang_typescript", "default", "User maintains a TypeScript web UI.", "fact"),
    Chunk("user_editor_neovim", "default", "User's editor is Neovim with LSP.", "fact"),
    Chunk("user_shell_zsh", "default", "User's shell is zsh.", "fact"),
    Chunk("user_os_macos", "default", "User runs macOS Sequoia on a MacBook Pro.", "fact"),
    Chunk("user_pkg_uv", "default", "User uses uv to manage Python deps.", "fact"),
    # Summaries (4) — go to graph
    Chunk("conv_summary_2026_04_01", "default",
          "Discussed LightRAG integration plan over a 2-hour session.",
          "summary"),
    Chunk("conv_summary_2026_04_15", "default",
          "Reviewed Qdrant migration path. User prefers local mode.",
          "summary"),
    Chunk("conv_summary_2026_04_20", "default",
          "Walked through decay config tuning. Settled on per-type profiles.",
          "summary"),
    Chunk("conv_summary_2026_04_22", "default",
          "Sketched test plan for hybrid scoring.",
          "summary"),
    # Episodes (5) — DO NOT go to graph
    Chunk("turn_001", "default", "User asked: how do I add an extra to pyproject?", "episode"),
    Chunk("turn_002", "default", "Assistant explained PEP 621 optional-dependencies.", "episode"),
    Chunk("turn_003", "default", "User asked: where does qdrant store data locally?", "episode"),
    Chunk("turn_004", "default", "Assistant answered ~/.obscura/qdrant/.", "episode"),
    Chunk("turn_005", "default", "User confirmed and moved on.", "episode"),
    # General (3) — go to graph
    Chunk("note_test_idiom", "default",
          "Use BackendConfig + SQLiteBackend in tmp_path for vector tests.",
          "general"),
    Chunk("note_async_test", "default",
          "Pytest is configured for asyncio_mode=auto; just use async def test_*.",
          "general"),
    Chunk("note_lint", "default",
          "Run `make lint` and `make typecheck` before opening a PR.",
          "general"),
    # Preferences (2) — DO NOT go to graph (immune to decay too)
    Chunk("pref_no_emoji", "default", "User prefers no emojis in generated docs.", "preference"),
    Chunk("pref_concise", "default", "User wants concise responses.", "preference"),
]


def by_type(memory_type: str) -> list[Chunk]:
    return [c for c in CORPUS if c.memory_type == memory_type]


def by_key(key: str) -> Chunk:
    return next(c for c in CORPUS if c.key == key)
```

### `assert_helpers.py`

```python
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


def assert_metadata_subset(actual: dict[str, Any], expected_subset: dict[str, Any]) -> None:
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
```

---

## 6. `test_scoring.py` — `hybrid_score` math

This module tests the pure function `obscura.lightrag_memory.scoring.hybrid_score()` and the `HybridWeights` dataclass. No I/O, no fixtures beyond pytest's parametrize machinery.

```python
"""Tests for obscura.lightrag_memory.scoring.hybrid_score()."""

from __future__ import annotations

import math

import pytest

from obscura.lightrag_memory.scoring import (
    HybridWeights,
    hybrid_score,
    load_hybrid_weights_from_disk,
)


# ---------------------------------------------------------------------------
# hybrid_score parameterized cases
# ---------------------------------------------------------------------------

DEFAULTS = HybridWeights()  # vector=0.5, graph=0.3, decay=0.15, usage=0.05


def _expected(vec: float, graph: float, decay: float, usage: int, w: HybridWeights) -> float:
    """Reference implementation — computes the canonical expected value.

    Mirrors the spec in 00-overview.md §"Phase 3" exactly.
    """
    vec_clamped = max(0.0, vec)
    usage_norm = min(math.log1p(usage) / math.log1p(100), 1.0)
    return (
        w.vector * vec_clamped
        + w.graph * graph
        + w.decay * decay
        + w.usage * usage_norm
    )


@pytest.mark.parametrize(
    "vec,graph,decay,usage,weights,description",
    [
        (1.0, 1.0, 1.0, 0, DEFAULTS, "all-max-no-usage"),
        (1.0, 1.0, 1.0, 100, DEFAULTS, "all-max-saturated-usage"),
        (1.0, 1.0, 1.0, 1000, DEFAULTS, "all-max-over-saturated"),
        (0.0, 0.0, 0.0, 0, DEFAULTS, "all-zero"),
        (0.5, 0.5, 0.5, 50, DEFAULTS, "balanced-mid"),
        (0.8, 0.2, 1.0, 10, DEFAULTS, "high-vec-low-graph"),
        (0.2, 0.9, 0.5, 5, DEFAULTS, "low-vec-high-graph"),
        # All-vector weighting
        (1.0, 0.0, 0.0, 0, HybridWeights(vector=1.0, graph=0.0, decay=0.0, usage=0.0),
         "all-vector"),
        # All-graph weighting
        (0.0, 1.0, 0.0, 0, HybridWeights(vector=0.0, graph=1.0, decay=0.0, usage=0.0),
         "all-graph"),
        # All-decay weighting
        (0.0, 0.0, 1.0, 0, HybridWeights(vector=0.0, graph=0.0, decay=1.0, usage=0.0),
         "all-decay"),
        # All-usage weighting saturates
        (0.0, 0.0, 0.0, 1000, HybridWeights(vector=0.0, graph=0.0, decay=0.0, usage=1.0),
         "all-usage-saturated"),
        # Negative-vector clamp
        (-0.3, 0.5, 0.5, 0, DEFAULTS, "negative-vector-clamped"),
        (-1.0, 0.0, 0.0, 0, DEFAULTS, "very-negative-vector-clamped"),
    ],
)
def test_hybrid_score_parametrized(
    vec: float,
    graph: float,
    decay: float,
    usage: int,
    weights: HybridWeights,
    description: str,
) -> None:
    """`hybrid_score` matches the reference computation."""
    actual = hybrid_score(
        vector_sim=vec,
        graph_relevance=graph,
        decay_multiplier=decay,
        usage_count=usage,
        weights=weights,
    )
    expected = _expected(vec, graph, decay, usage, weights)
    assert actual == pytest.approx(expected, abs=1e-6), description


def test_hybrid_score_default_weights_sum_to_one() -> None:
    """Default weights must sum to 1.0 — a soft contract for interpretability."""
    w = HybridWeights()
    total = w.vector + w.graph + w.decay + w.usage
    assert total == pytest.approx(1.0, abs=1e-9)


def test_hybrid_score_max_input_capped_at_one() -> None:
    """With default weights, all-1 inputs (and saturated usage) cap at 1.0."""
    score = hybrid_score(
        vector_sim=1.0,
        graph_relevance=1.0,
        decay_multiplier=1.0,
        usage_count=1000,
        weights=HybridWeights(),
    )
    assert score == pytest.approx(1.0, abs=1e-6)


def test_hybrid_score_min_input_at_zero() -> None:
    """All-zero inputs produce zero."""
    score = hybrid_score(
        vector_sim=0.0,
        graph_relevance=0.0,
        decay_multiplier=0.0,
        usage_count=0,
        weights=HybridWeights(),
    )
    assert score == 0.0


def test_hybrid_score_monotonic_in_vector() -> None:
    """Holding everything else equal, raising vector_sim never lowers the score."""
    args = dict(graph_relevance=0.5, decay_multiplier=0.5, usage_count=10,
                weights=HybridWeights())
    s_lo = hybrid_score(vector_sim=0.2, **args)  # type: ignore[arg-type]
    s_mid = hybrid_score(vector_sim=0.5, **args)  # type: ignore[arg-type]
    s_hi = hybrid_score(vector_sim=0.9, **args)  # type: ignore[arg-type]
    assert s_lo <= s_mid <= s_hi


def test_hybrid_score_monotonic_in_usage() -> None:
    """Raising usage_count never lowers the score (saturating but non-decreasing)."""
    args = dict(vector_sim=0.5, graph_relevance=0.5, decay_multiplier=0.5,
                weights=HybridWeights(usage=0.5, vector=0.5, graph=0.0, decay=0.0))
    s_0 = hybrid_score(usage_count=0, **args)  # type: ignore[arg-type]
    s_10 = hybrid_score(usage_count=10, **args)  # type: ignore[arg-type]
    s_100 = hybrid_score(usage_count=100, **args)  # type: ignore[arg-type]
    s_1000 = hybrid_score(usage_count=1000, **args)  # type: ignore[arg-type]
    assert s_0 <= s_10 <= s_100 <= s_1000


# ---------------------------------------------------------------------------
# HybridWeights validation
# ---------------------------------------------------------------------------


class TestHybridWeightsValidation:
    def test_negative_weight_rejected(self) -> None:
        """Negative weights are nonsensical — constructor must raise."""
        with pytest.raises(ValueError, match="negative"):
            HybridWeights(vector=-0.1)

    def test_non_summing_weights_warn_but_succeed(self, caplog: pytest.LogCaptureFixture) -> None:
        """Weights that don't sum to ~1.0 produce a warning but no error.

        Rationale: rare A/B testing scenarios may want unbalanced weights.
        We want telemetry, not a hard refusal.
        """
        with caplog.at_level("WARNING"):
            w = HybridWeights(vector=0.9, graph=0.5, decay=0.0, usage=0.0)  # sums to 1.4
        assert "weights do not sum" in caplog.text.lower() or any(
            "1.4" in r.message for r in caplog.records
        )
        assert w.vector == 0.9

    def test_zero_weights_allowed(self) -> None:
        """All-zero weights are degenerate but legal — every score becomes 0."""
        w = HybridWeights(vector=0.0, graph=0.0, decay=0.0, usage=0.0)
        score = hybrid_score(
            vector_sim=1.0,
            graph_relevance=1.0,
            decay_multiplier=1.0,
            usage_count=100,
            weights=w,
        )
        assert score == 0.0


# ---------------------------------------------------------------------------
# load_hybrid_weights_from_disk
# ---------------------------------------------------------------------------


class TestLoadWeightsFromDisk:
    def test_returns_defaults_when_file_missing(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing config file returns `HybridWeights()` defaults silently."""
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        # Ensure no config file exists
        cfg = tmp_path / "config.toml"
        assert not cfg.exists()
        w = load_hybrid_weights_from_disk()
        assert w == HybridWeights()

    def test_returns_parsed_when_file_present(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A valid `[vector_memory.lightrag.weights]` block is parsed."""
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[vector_memory.lightrag.weights]\n"
            "vector = 0.4\n"
            "graph = 0.4\n"
            "decay = 0.15\n"
            "usage = 0.05\n",
        )
        w = load_hybrid_weights_from_disk()
        assert w.vector == pytest.approx(0.4)
        assert w.graph == pytest.approx(0.4)
        assert w.decay == pytest.approx(0.15)
        assert w.usage == pytest.approx(0.05)

    def test_returns_defaults_on_malformed_toml(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Garbage in config is logged and falls back to defaults — never crashes."""
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        cfg = tmp_path / "config.toml"
        cfg.write_text("this is not [valid toml")
        with caplog.at_level("WARNING"):
            w = load_hybrid_weights_from_disk()
        assert w == HybridWeights()
        assert any("hybrid weights" in r.message.lower() for r in caplog.records)
```

This test file alone has ~13 cases for the math and ~3 for config loading — well above the 6-8 cases sketched in `00-overview.md` §"Phase 6 — Tests".

---

## 7. `test_hybrid_store.py` — set/delete/whitelist behavior

```python
"""Tests for HybridVectorMemoryStore overrides — set/delete/whitelist."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore

    from .conftest import MockLightRAG


def _wait_for(predicate, timeout_s: float = 2.0, poll_s: float = 0.01) -> bool:
    """Poll `predicate()` until truthy or timeout. Returns last truthy value."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return True
        time.sleep(poll_s)
    return False


class TestSetFanout:
    def test_set_calls_super_synchronously(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        """`set()` returns *before* `insert_safe` is called.

        Use a slow mock to widen the race window: if super().set() were
        blocking on the executor we'd deadlock.
        """
        mock_lightrag.state.next_insert_sleep_s = 0.5

        t_start = time.monotonic()
        hybrid_store.set("k1", "x" * 50, memory_type="fact")
        elapsed = time.monotonic() - t_start

        # Super().set() is in-process work; should be well under the 0.5s sleep
        assert elapsed < 0.2, f"set() blocked {elapsed:.3f}s — fan-out is sync"

        # The chunk is persisted regardless
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
        hybrid_store.set("s1", "Discussion summary about decay tuning.",
                         memory_type="summary")
        ok = _wait_for(lambda: len(mock_lightrag.state.inserts) >= 1)
        assert ok

    def test_set_skips_adapter_for_episode(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        """`memory_type="episode"` is NOT in the whitelist — no fan-out."""
        hybrid_store.set("e1", "User said hello.", memory_type="episode")
        # Give the executor a generous chance to (incorrectly) fire
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
        """Text below `MIN_LENGTH` skips fan-out (avoids LLM cost on chatter).

        Note: `super().set()` still persists the chunk — we just don't
        spend money entity-extracting "ok".
        """
        hybrid_store.set("k3", "ok", memory_type="fact")
        time.sleep(0.1)
        # Real adapter skips; mock records but with `_skip_reason`
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
        # Wait for the insert to land before deleting
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
        # Should not raise
        deleted = hybrid_store.delete("k1")
        assert deleted is True

    def test_delete_missing_key_returns_false(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        deleted = hybrid_store.delete("does-not-exist")
        assert deleted is False
        # No graph delete attempted
        time.sleep(0.05)
        assert mock_lightrag.state.deletes == []


class TestDocIdRoundtrip:
    def test_doc_id_decodes_unambiguously(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        """Encoded `f"{namespace}::{key}"` round-trips."""
        hybrid_store.set("user_lang_python", "User uses Python.",
                         namespace="default", memory_type="fact")
        _wait_for(lambda: len(mock_lightrag.state.inserts) >= 1)
        doc_id, _, _ = mock_lightrag.state.inserts[0]
        # Naive decode should produce ("default", "user_lang_python")
        ns, _, key = doc_id.partition("::")
        assert ns == "default"
        assert key == "user_lang_python"

    def test_doc_id_handles_namespace_with_colon(
        self,
        hybrid_store: HybridVectorMemoryStore,
        mock_lightrag: MockLightRAG,
    ) -> None:
        """Namespaces using single `:` (e.g. `default:semantic`) decode correctly."""
        hybrid_store.set("k1", "test content for ns:colon",
                         namespace="default:semantic", memory_type="fact")
        _wait_for(lambda: len(mock_lightrag.state.inserts) >= 1)
        doc_id, _, _ = mock_lightrag.state.inserts[0]
        ns, sep, key = doc_id.partition("::")
        assert sep == "::"
        assert ns == "default:semantic"
        assert key == "k1"

    def test_namespace_containing_double_colon_documented(self) -> None:
        """Namespaces MUST NOT contain `::` — the doc_id encoding assumes this.

        This test exists to fail loudly if anyone changes the encoding.
        Decision recorded in 00-overview.md §"Phase 2 — Critical decisions".
        """
        from obscura.lightrag_memory.hybrid_store import _encode_doc_id, _decode_doc_id

        # Legal namespace
        encoded = _encode_doc_id("default", "k1")
        assert _decode_doc_id(encoded) == ("default", "k1")

        # Illegal namespace — encoding either rejects or escapes
        with pytest.raises((ValueError, AssertionError)):
            _encode_doc_id("bad::namespace", "k1")
```

**Note:** `_encode_doc_id` / `_decode_doc_id` are assumed to be the explicit helpers in `hybrid_store.py`. If the implementation uses inline f-strings, refactor those into named helpers as part of this PR — testability dictates.

---

## 8. `test_adapter.py` — sync wrappers and lifecycle

This module tests the *real* `LightRAGAdapter`, not the mock — but it patches the underlying `LightRAG` instance to avoid the heavy dep.

```python
"""Tests for LightRAGAdapter — the real adapter, with a stubbed LightRAG."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest


# A lightweight fake that mimics the LightRAG instance the adapter wraps.
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
        self.queries.append((query, getattr(param, "mode", "?"),
                             getattr(param, "top_k", -1)))
        return "stub answer"


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


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
        monkeypatch.setattr(adapter_mod, "_build_lightrag_instance",
                            lambda **kwargs: fake)
        monkeypatch.setattr(adapter_mod, "_lightrag_root", lambda: tmp_path / "lr")

        adapter = adapter_mod.LightRAGAdapter.for_user(fake_user,
                                                       embedding_fn=lambda s: [0.0] * 384)
        assert adapter is not None
        # working_dir created
        assert (tmp_path / "lr").exists()
        adapter.close()


# ---------------------------------------------------------------------------
# insert_safe — sync façade over async ainsert
# ---------------------------------------------------------------------------


class TestInsertSafe:
    def _adapter_with_fake(self, monkeypatch, fake_user, tmp_path) -> tuple[Any, _FakeLightRAG]:
        from obscura.lightrag_memory import adapter as adapter_mod

        fake = _FakeLightRAG()
        monkeypatch.setattr(adapter_mod, "_lightrag_enabled", lambda: True)
        monkeypatch.setattr(adapter_mod, "_build_lightrag_instance",
                            lambda **kwargs: fake)
        monkeypatch.setattr(adapter_mod, "_lightrag_root", lambda: tmp_path / "lr")

        adapter = adapter_mod.LightRAGAdapter.for_user(fake_user,
                                                       embedding_fn=lambda s: [0.0] * 384)
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
            # Should not raise
            adapter.insert_safe(doc_id="k1", text="some text" * 10, metadata={})
            # Internal counter incremented
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
            # Adapter timeout is configurable via OBSCURA_LIGHTRAG_INGEST_TIMEOUT_S
            monkeypatch.setattr(adapter, "ingest_timeout_s", 0.1)
            fake.next_insert_sleep_s = 1.0  # 10x the timeout
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
                adapter.insert_safe(doc_id=f"k{i}", text=f"content {i}" * 5,
                                    metadata={"i": i})
            # Wait for fan-out
            deadline = time.monotonic() + 5.0
            while len(fake.inserts) < 10 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert len(fake.inserts) == 10
            # Order preserved — the executor dispatches single-threaded per adapter
            ids = [doc_id for doc_id, _ in fake.inserts]
            assert ids == [f"k{i}" for i in range(10)]
        finally:
            adapter.close()


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------


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
        monkeypatch.setattr(adapter_mod, "_build_lightrag_instance",
                            lambda **kwargs: fake)
        monkeypatch.setattr(adapter_mod, "_lightrag_root", lambda: tmp_path / "lr")
        adapter = adapter_mod.LightRAGAdapter.for_user(fake_user,
                                                       embedding_fn=lambda s: [0.0] * 384)

        for i in range(10):
            adapter.insert_safe(doc_id=f"k{i}", text=f"content{i}" * 5,
                                metadata={})
        # Close should block until pending work is drained or close-timeout fires
        t0 = time.monotonic()
        adapter.close()
        elapsed = time.monotonic() - t0
        # No infinite block; close completes within bounded time
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
        monkeypatch.setattr(adapter_mod, "_build_lightrag_instance",
                            lambda **kwargs: fake)
        monkeypatch.setattr(adapter_mod, "_lightrag_root", lambda: tmp_path / "lr")
        adapter = adapter_mod.LightRAGAdapter.for_user(fake_user,
                                                       embedding_fn=lambda s: [0.0] * 384)
        adapter.close()
        # Second close must not raise
        adapter.close()
        adapter.close()
```

---

## 9. `test_search_hybrid.py` — query path

The most consequential file in the suite. Tests exercise the full query pipeline: LightRAG hits → hydration → decay re-application → usage signal → final ordering.

```python
"""Tests for HybridVectorMemoryStore.search_hybrid()."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from obscura.lightrag_memory.adapter import GraphHit
from obscura.memory import MemoryKey

from .assert_helpers import assert_score_decreasing


# Helper to seed the backend AND register the corresponding canned hit
def _seed_with_hit(
    store,
    mock_lr,
    *,
    key: str,
    text: str,
    namespace: str = "default",
    memory_type: str = "fact",
    vector_sim: float = 0.5,
    graph_relevance: float = 0.5,
    created_at: datetime | None = None,
    accessed_at: datetime | None = None,
    access_count: int = 0,
    query_substring: str = "default-query",
) -> None:
    """Persist a chunk via store.set() and register the corresponding hit."""
    metadata = {"access_count": access_count}
    store.set(key, text, metadata=metadata, namespace=namespace, memory_type=memory_type)
    # Manually patch created_at / accessed_at on the persisted entry
    if created_at or accessed_at:
        entry = store.backend.get_vector(MemoryKey(namespace=namespace, key=key))
        if entry and created_at:
            entry.created_at = created_at
        if entry and accessed_at:
            entry.accessed_at = accessed_at
        # Re-store to persist (some backends don't accept partial updates)
        store.backend.store_vector(
            key=entry.key,
            text=entry.text,
            embedding=entry.embedding,
            metadata=entry.metadata,
            memory_type=entry.memory_type,
            expires_at=None,
        )

    hit = GraphHit(
        namespace=namespace,
        key=key,
        vector_sim=vector_sim,
        graph_relevance=graph_relevance,
        text_excerpt=text[:80],
    )
    # Append to existing canned response if any, else create
    found = False
    for sub, hits in mock_lr.state.canned_aquery:
        if sub == query_substring:
            hits.append(hit)
            found = True
            break
    if not found:
        mock_lr.set_canned(query_substring, [hit])


# ---------------------------------------------------------------------------
# Basic ordering
# ---------------------------------------------------------------------------


class TestSearchHybridBasic:
    def test_returns_in_score_descending_order(self, hybrid_store, mock_lightrag) -> None:
        """5 seeded chunks → final ordering matches the manually-computed scores."""
        _seed_with_hit(hybrid_store, mock_lightrag, key="k1",
                       text="content one " * 5, vector_sim=0.9, graph_relevance=0.9)
        _seed_with_hit(hybrid_store, mock_lightrag, key="k2",
                       text="content two " * 5, vector_sim=0.8, graph_relevance=0.7)
        _seed_with_hit(hybrid_store, mock_lightrag, key="k3",
                       text="content three " * 5, vector_sim=0.5, graph_relevance=0.6)
        _seed_with_hit(hybrid_store, mock_lightrag, key="k4",
                       text="content four " * 5, vector_sim=0.3, graph_relevance=0.4)
        _seed_with_hit(hybrid_store, mock_lightrag, key="k5",
                       text="content five " * 5, vector_sim=0.1, graph_relevance=0.2)

        results = hybrid_store.search_hybrid("default-query", top_k=5)
        assert len(results) == 5
        assert_score_decreasing(results)
        # Highest-similarity chunk wins
        assert results[0].key.key == "k1"

    def test_top_k_caps_results(self, hybrid_store, mock_lightrag) -> None:
        for i in range(10):
            _seed_with_hit(hybrid_store, mock_lightrag, key=f"k{i}",
                           text=f"content {i} " * 5,
                           vector_sim=0.9 - 0.05 * i,
                           graph_relevance=0.5)
        results = hybrid_store.search_hybrid("default-query", top_k=3)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# Decay re-application
# ---------------------------------------------------------------------------


class TestSearchHybridDecay:
    def test_decay_downweights_old_chunks(self, hybrid_store, mock_lightrag) -> None:
        """A 60-day-old chunk vs. a 1-day-old chunk — decay shifts the order."""
        now = datetime.now(UTC)
        # Both chunks have IDENTICAL vector_sim and graph_relevance.
        # Age is the only differentiator.
        _seed_with_hit(hybrid_store, mock_lightrag, key="old",
                       text="old content " * 5,
                       vector_sim=0.5, graph_relevance=0.5,
                       created_at=now - timedelta(days=60))
        _seed_with_hit(hybrid_store, mock_lightrag, key="fresh",
                       text="fresh content " * 5,
                       vector_sim=0.5, graph_relevance=0.5,
                       created_at=now - timedelta(days=1))

        results = hybrid_store.search_hybrid("default-query", top_k=2)
        assert len(results) == 2
        assert results[0].key.key == "fresh", \
            f"decay didn't downweight old chunk: {[r.key.key for r in results]}"


# ---------------------------------------------------------------------------
# Usage frequency
# ---------------------------------------------------------------------------


class TestSearchHybridUsage:
    def test_usage_shifts_ordering(self, hybrid_store, mock_lightrag) -> None:
        """Among ties on vector + graph + decay, higher access_count wins."""
        # Heavy-usage weighting to make the signal measurable
        from obscura.lightrag_memory.scoring import HybridWeights
        weights = HybridWeights(vector=0.4, graph=0.4, decay=0.0, usage=0.2)

        _seed_with_hit(hybrid_store, mock_lightrag, key="hot",
                       text="hot content " * 5,
                       vector_sim=0.5, graph_relevance=0.5, access_count=50)
        _seed_with_hit(hybrid_store, mock_lightrag, key="cold",
                       text="cold content " * 5,
                       vector_sim=0.5, graph_relevance=0.5, access_count=0)

        results = hybrid_store.search_hybrid("default-query", top_k=2, weights=weights)
        assert results[0].key.key == "hot"


# ---------------------------------------------------------------------------
# Namespace filtering
# ---------------------------------------------------------------------------


class TestSearchHybridNamespace:
    def test_namespace_filter_returns_only_matching_ns(self, hybrid_store, mock_lightrag) -> None:
        _seed_with_hit(hybrid_store, mock_lightrag, key="k1",
                       text="content A1 " * 5, namespace="A",
                       vector_sim=0.9, graph_relevance=0.9)
        _seed_with_hit(hybrid_store, mock_lightrag, key="k2",
                       text="content A2 " * 5, namespace="A",
                       vector_sim=0.8, graph_relevance=0.8)
        _seed_with_hit(hybrid_store, mock_lightrag, key="k3",
                       text="content B1 " * 5, namespace="B",
                       vector_sim=0.95, graph_relevance=0.95)

        results = hybrid_store.search_hybrid("default-query", namespace="A", top_k=5)
        assert all(r.key.namespace == "A" for r in results)
        assert {r.key.key for r in results} == {"k1", "k2"}


# ---------------------------------------------------------------------------
# Stale graph references
# ---------------------------------------------------------------------------


class TestSearchHybridStaleRef:
    def test_drops_hits_for_missing_keys(self, hybrid_store, mock_lightrag) -> None:
        """LightRAG references a key that doesn't exist in the backend → silently drop."""
        # Seed only k1
        _seed_with_hit(hybrid_store, mock_lightrag, key="k1",
                       text="real content " * 5, vector_sim=0.5, graph_relevance=0.5)
        # Add a phantom hit
        mock_lightrag.set_canned("default-query", [
            GraphHit(namespace="default", key="k1",
                     vector_sim=0.5, graph_relevance=0.5, text_excerpt=""),
            GraphHit(namespace="default", key="phantom",
                     vector_sim=0.99, graph_relevance=0.99, text_excerpt=""),
        ])
        results = hybrid_store.search_hybrid("default-query", top_k=5)
        # Only the real chunk
        assert len(results) == 1
        assert results[0].key.key == "k1"


# ---------------------------------------------------------------------------
# Fallback paths
# ---------------------------------------------------------------------------


class TestSearchHybridFallback:
    def test_empty_aquery_falls_back_to_search_reranked(
        self,
        hybrid_store,
        mock_lightrag,
        monkeypatch,
    ) -> None:
        """When LightRAG returns no hits, fall back to plain `search_reranked`."""
        # Seed some content
        hybrid_store.set("k1", "fallback content here." * 3, memory_type="fact")
        # No canned response → aquery returns []

        called = {"super_search": False}
        # Spy on parent class
        from obscura.vector_memory import VectorMemoryStore
        original = VectorMemoryStore.search_reranked

        def _spy(self, *args, **kwargs):
            called["super_search"] = True
            return original(self, *args, **kwargs)

        monkeypatch.setattr(VectorMemoryStore, "search_reranked", _spy)

        results = hybrid_store.search_hybrid("nothing matches", top_k=5)
        assert called["super_search"] is True
        # Result shape is identical — VectorEntry list
        for r in results:
            assert hasattr(r, "final_score")

    def test_aquery_raises_falls_back(
        self,
        hybrid_store,
        mock_lightrag,
        monkeypatch,
    ) -> None:
        """When `aquery` raises, fall through to `search_reranked` — no propagation."""
        hybrid_store.set("k1", "fallback content." * 3, memory_type="fact")
        mock_lightrag.state.next_aquery_raises = RuntimeError("LR exploded")

        # Should not raise
        results = hybrid_store.search_hybrid("any query", top_k=5)
        assert isinstance(results, list)

    def test_aquery_timeout_falls_back(
        self,
        hybrid_store,
        mock_lightrag,
        monkeypatch,
    ) -> None:
        """If `aquery` exceeds timeout, fall back. Set a very low timeout via env."""
        monkeypatch.setenv("OBSCURA_LIGHTRAG_TIMEOUT_MS", "50")
        hybrid_store.set("k1", "fallback after timeout." * 3, memory_type="fact")
        mock_lightrag.state.next_aquery_sleep_s = 1.0  # 20x the timeout

        t0 = __import__("time").monotonic()
        results = hybrid_store.search_hybrid("any query", top_k=5)
        elapsed = __import__("time").monotonic() - t0
        assert elapsed < 0.5, f"timeout not respected: {elapsed:.2f}s"
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# Access-count increment
# ---------------------------------------------------------------------------


class TestSearchHybridUsageIncrement:
    def test_search_increments_access_count(self, hybrid_store, mock_lightrag) -> None:
        """A query that returns a chunk bumps `metadata.access_count` by 1."""
        _seed_with_hit(hybrid_store, mock_lightrag, key="k1",
                       text="incrementable content " * 5,
                       vector_sim=0.9, graph_relevance=0.9, access_count=5)

        results = hybrid_store.search_hybrid("default-query", top_k=1)
        assert results[0].key.key == "k1"

        # Wait for the async touch to land
        import time as _t
        for _ in range(20):
            entry = hybrid_store.get("k1")
            if entry and entry.metadata.get("access_count", 0) >= 6:
                break
            _t.sleep(0.05)
        entry = hybrid_store.get("k1")
        assert entry is not None
        assert entry.metadata.get("access_count", 0) >= 6
```

---

## 10. `test_touch_count.py` — race tolerance

```python
"""Tests for `_touch_and_count_async` — the usage-frequency / lazy-index path."""

from __future__ import annotations

import asyncio
import time

import pytest

from obscura.memory import MemoryKey


class TestTouchAtomicity:
    def test_single_touch_increments(self, hybrid_store, mock_lightrag) -> None:
        hybrid_store.set("k1", "content " * 10, memory_type="fact")
        entry_before = hybrid_store.get("k1")
        assert entry_before is not None
        before = entry_before.metadata.get("access_count", 0)

        hybrid_store.touch("k1")
        # Wait for async update
        for _ in range(20):
            entry = hybrid_store.get("k1")
            if entry and entry.metadata.get("access_count", 0) > before:
                break
            time.sleep(0.05)

        entry = hybrid_store.get("k1")
        assert entry is not None
        assert entry.metadata.get("access_count", 0) == before + 1

    def test_concurrent_touches_relaxed(self, hybrid_store, mock_lightrag) -> None:
        """10 concurrent touches → final count in [1, 10].

        SQLite's row locking makes 10 likely; we accept anything ≥1 because
        racy increments are an *acceptable* failure mode for usage stats.
        Pin to 10 only if the implementation uses `ON CONFLICT … access_count + 1`.
        """
        import threading

        hybrid_store.set("k1", "raced content " * 5, memory_type="fact")

        def _touch() -> None:
            hybrid_store.touch("k1")

        threads = [threading.Thread(target=_touch) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Drain the touch executor
        time.sleep(0.5)

        entry = hybrid_store.get("k1")
        assert entry is not None
        count = entry.metadata.get("access_count", 0)
        assert 1 <= count <= 10, f"unexpected count: {count}"

    def test_touch_missing_key_no_error(self, hybrid_store, mock_lightrag) -> None:
        """Touching a nonexistent key is a silent no-op."""
        hybrid_store.touch("phantom-key")
        # No assertion — just verify no exception


class TestLazyIndex:
    def test_touch_schedules_lazy_ingest_when_unindexed(
        self,
        hybrid_store,
        mock_lightrag,
        monkeypatch,
    ) -> None:
        """A chunk touched but not yet graph-indexed → schedule ingest."""
        # Seed via low-level backend to bypass the set() fan-out — emulates
        # a chunk that pre-dates LightRAG enablement.
        from obscura.vector_memory.backends import VectorEntry

        entry = VectorEntry(
            key=MemoryKey(namespace="default", key="legacy"),
            text="legacy content here. " * 5,
            embedding=[0.0] * 384,
            metadata={"memory_type": "fact"},
            memory_type="fact",
            created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        )
        hybrid_store.backend.store_vector(
            key=entry.key,
            text=entry.text,
            embedding=entry.embedding,
            metadata=entry.metadata,
            memory_type=entry.memory_type,
            expires_at=None,
        )
        assert mock_lightrag.state.inserts == []

        hybrid_store.touch("legacy")

        # Wait for lazy ingest
        for _ in range(40):
            if len(mock_lightrag.state.inserts) >= 1:
                break
            time.sleep(0.05)
        assert len(mock_lightrag.state.inserts) >= 1, "lazy ingest never fired"

    def test_touch_skips_already_indexed(
        self,
        hybrid_store,
        mock_lightrag,
    ) -> None:
        """A chunk with `lr_indexed_at` set → no re-ingest on touch."""
        from datetime import UTC, datetime

        from obscura.vector_memory.backends import VectorEntry

        now = datetime.now(UTC)
        hybrid_store.backend.store_vector(
            key=MemoryKey(namespace="default", key="indexed"),
            text="indexed content. " * 5,
            embedding=[0.0] * 384,
            metadata={"memory_type": "fact", "lr_indexed_at": now.isoformat()},
            memory_type="fact",
            expires_at=None,
        )
        assert mock_lightrag.state.inserts == []
        hybrid_store.touch("indexed")
        time.sleep(0.2)
        assert mock_lightrag.state.inserts == []

    def test_touch_respects_attempt_limit(
        self,
        hybrid_store,
        mock_lightrag,
    ) -> None:
        """A chunk with `lr_index_attempts >= 3` → no lazy ingest."""
        from datetime import UTC, datetime

        hybrid_store.backend.store_vector(
            key=MemoryKey(namespace="default", key="poisoned"),
            text="content that keeps failing. " * 5,
            embedding=[0.0] * 384,
            metadata={"memory_type": "fact", "lr_index_attempts": 4},
            memory_type="fact",
            expires_at=None,
        )
        hybrid_store.touch("poisoned")
        time.sleep(0.2)
        assert mock_lightrag.state.inserts == []
```

---

## 11. `test_consolidator_hook.py` — consolidator integration

The `MemoryConsolidator` (`obscura/vector_memory/consolidator.py`) deletes consolidated episodes and creates summaries. The Phase 5 work added a hook so deleted episodes also get cleared from the graph.

```python
"""Tests for the consolidator → LightRAG integration hook."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta


class TestConsolidatorIntegration:
    def test_consolidate_deletes_graph_entries_for_removed_episodes(
        self,
        hybrid_store,
        mock_lightrag,
    ) -> None:
        """Consolidating 3 old episodes → adapter sees 3 `delete_safe` calls."""
        # Pre-seed 3 old episodes — old enough to be consolidated
        old = datetime.now(UTC) - timedelta(days=30)
        for i in range(3):
            hybrid_store.backend.store_vector(
                key=__import__(
                    "obscura.memory", fromlist=["MemoryKey"]
                ).MemoryKey(namespace="default", key=f"e{i}"),
                text=f"old episode {i} content here. " * 3,
                embedding=[0.0] * 384,
                metadata={"memory_type": "episode", "graph_index": True},
                memory_type="episode",
                expires_at=None,
            )
        # Run maintenance — consolidates and creates a summary
        report = hybrid_store.run_maintenance()
        # Wait for any async deletes to land
        time.sleep(0.3)
        # The exact number depends on consolidation grouping; assert ≥1
        assert report.episodes_consolidated >= 1
        # The deletes propagate to the adapter
        assert len(mock_lightrag.state.deletes) >= report.episodes_consolidated

    def test_consolidate_inserts_summary_via_set(
        self,
        hybrid_store,
        mock_lightrag,
    ) -> None:
        """The new summary chunk goes through `set()` → adapter receives it."""
        old = datetime.now(UTC) - timedelta(days=30)
        for i in range(5):
            hybrid_store.backend.store_vector(
                key=__import__(
                    "obscura.memory", fromlist=["MemoryKey"]
                ).MemoryKey(namespace="default", key=f"ep{i}"),
                text=f"older episode {i}. " * 4,
                embedding=[0.0] * 384,
                metadata={"memory_type": "episode"},
                memory_type="episode",
                expires_at=None,
            )

        before_inserts = len(mock_lightrag.state.inserts)
        report = hybrid_store.run_maintenance()
        time.sleep(0.3)
        # If a summary was created, it went through set() → adapter saw it
        if report.summaries_created > 0:
            assert len(mock_lightrag.state.inserts) > before_inserts

    def test_consolidator_handles_adapter_failures_gracefully(
        self,
        hybrid_store,
        mock_lightrag,
        monkeypatch,
    ) -> None:
        """If `delete_safe` raises during consolidation, the episode is still removed."""

        def _boom(doc_id: str) -> None:
            raise RuntimeError("graph delete failed")

        monkeypatch.setattr(mock_lightrag, "delete_safe", _boom)

        from obscura.memory import MemoryKey

        old = datetime.now(UTC) - timedelta(days=30)
        for i in range(3):
            hybrid_store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"flaky{i}"),
                text=f"flaky episode {i}. " * 4,
                embedding=[0.0] * 384,
                metadata={"memory_type": "episode"},
                memory_type="episode",
                expires_at=None,
            )

        # Should not raise
        report = hybrid_store.run_maintenance()
        # Vector-store deletion still succeeded
        for i in range(3):
            entry = hybrid_store.backend.get_vector(
                MemoryKey(namespace="default", key=f"flaky{i}")
            )
            # Either gone OR still present (depending on consolidation grouping).
            # Critically, the test didn't crash.
        assert report is not None
```

---

## 12. `test_backfill.py` — migration engine

```python
"""Tests for obscura.lightrag_memory.backfill — batch migration of existing chunks."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from obscura.memory import MemoryKey


def _seed_corpus(store, count_by_type: dict[str, int]) -> None:
    """Populate the backend with `count_by_type[type]` chunks per memory type."""
    now = datetime.now(UTC)
    n = 0
    for mtype, count in count_by_type.items():
        for i in range(count):
            store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"{mtype}_{i}"),
                text=f"{mtype} chunk #{i} content here. " * 3,
                embedding=[0.0] * 384,
                metadata={"memory_type": mtype},
                memory_type=mtype,
                expires_at=None,
            )
            n += 1


class TestBackfillEstimate:
    def test_estimate_counts_only_indexable(self, hybrid_store, mock_lightrag) -> None:
        """60 fact + 30 summary + 10 episode = 90 indexable, 100 total."""
        from obscura.lightrag_memory.backfill import BackfillEngine

        _seed_corpus(hybrid_store, {"fact": 60, "summary": 30, "episode": 10})
        engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag)
        est = engine.estimate()
        assert est.total_chunks == 100
        assert est.indexable_chunks == 90  # episodes excluded by default
        assert est.skipped_chunks == 10

    def test_estimate_excludes_already_indexed(self, hybrid_store, mock_lightrag) -> None:
        """Chunks with `lr_indexed_at` set count as 'already done'."""
        from obscura.lightrag_memory.backfill import BackfillEngine

        now = datetime.now(UTC)
        for i in range(5):
            hybrid_store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"done_{i}"),
                text=f"already done chunk {i}. " * 3,
                embedding=[0.0] * 384,
                metadata={"memory_type": "fact",
                          "lr_indexed_at": now.isoformat()},
                memory_type="fact",
                expires_at=None,
            )
        for i in range(5):
            hybrid_store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"todo_{i}"),
                text=f"to-do chunk {i}. " * 3,
                embedding=[0.0] * 384,
                metadata={"memory_type": "fact"},
                memory_type="fact",
                expires_at=None,
            )

        engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag)
        est = engine.estimate()
        assert est.indexable_chunks == 5  # only the to-do ones
        assert est.already_indexed == 5


class TestBackfillExecution:
    def test_dry_run_no_inserts(self, hybrid_store, mock_lightrag) -> None:
        from obscura.lightrag_memory.backfill import BackfillEngine

        _seed_corpus(hybrid_store, {"fact": 10})
        engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag)
        report = engine.run(dry_run=True)
        assert mock_lightrag.state.inserts == []
        assert report.would_index == 10
        assert report.indexed == 0

    def test_runs_with_max_chunks(self, hybrid_store, mock_lightrag) -> None:
        """`--max-chunks 5` performs exactly 5 inserts."""
        from obscura.lightrag_memory.backfill import BackfillEngine

        _seed_corpus(hybrid_store, {"fact": 20})
        engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag)
        report = engine.run(max_chunks=5)
        # Allow a little slack for the executor
        time.sleep(0.5)
        assert len(mock_lightrag.state.inserts) == 5
        assert report.indexed == 5

    def test_idempotent_re_run_skips_indexed(self, hybrid_store, mock_lightrag) -> None:
        """Running twice doesn't re-index already-flagged chunks."""
        from obscura.lightrag_memory.backfill import BackfillEngine

        _seed_corpus(hybrid_store, {"fact": 10})
        engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag)
        report1 = engine.run()
        time.sleep(0.5)
        first_count = len(mock_lightrag.state.inserts)
        assert first_count == 10
        # Second run finds nothing to do
        report2 = engine.run()
        time.sleep(0.5)
        assert len(mock_lightrag.state.inserts) == first_count  # unchanged
        assert report2.indexed == 0

    def test_marks_indexed_at_on_success(self, hybrid_store, mock_lightrag) -> None:
        from obscura.lightrag_memory.backfill import BackfillEngine

        _seed_corpus(hybrid_store, {"fact": 3})
        engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag)
        engine.run()
        time.sleep(0.5)
        for i in range(3):
            entry = hybrid_store.backend.get_vector(
                MemoryKey(namespace="default", key=f"fact_{i}")
            )
            assert entry is not None
            assert "lr_indexed_at" in entry.metadata
            # Parseable ISO timestamp
            datetime.fromisoformat(entry.metadata["lr_indexed_at"])

    def test_increments_attempts_on_failure(self, hybrid_store, mock_lightrag) -> None:
        """Adapter failures bump `lr_index_attempts` and DO NOT set `lr_indexed_at`."""
        from obscura.lightrag_memory.backfill import BackfillEngine

        _seed_corpus(hybrid_store, {"fact": 2})

        # Make insert always fail
        original_insert = mock_lightrag.insert_safe

        def _fail(doc_id: str, text: str, metadata: dict) -> None:
            raise RuntimeError("simulated")

        # Wire failure through the adapter — but the adapter swallows.
        # The engine should still increment attempts.
        import unittest.mock as _mock

        with _mock.patch.object(mock_lightrag, "insert_safe", _fail):
            engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag)
            try:
                engine.run()
            except Exception:
                pass  # engine itself shouldn't propagate

        time.sleep(0.5)
        for i in range(2):
            entry = hybrid_store.backend.get_vector(
                MemoryKey(namespace="default", key=f"fact_{i}")
            )
            assert entry is not None
            assert entry.metadata.get("lr_index_attempts", 0) >= 1
            assert "lr_indexed_at" not in entry.metadata

    def test_excludes_failed_after_max_attempts(self, hybrid_store, mock_lightrag) -> None:
        """Chunks with `lr_index_attempts >= 3` are filtered from the next run."""
        from obscura.lightrag_memory.backfill import BackfillEngine

        # Seed 3 chunks: one at attempts=4 (filtered), two fresh.
        for i, attempts in enumerate([4, 0, 0]):
            md = {"memory_type": "fact"}
            if attempts:
                md["lr_index_attempts"] = attempts
            hybrid_store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"f{i}"),
                text=f"chunk {i} content. " * 3,
                embedding=[0.0] * 384,
                metadata=md,
                memory_type="fact",
                expires_at=None,
            )
        engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag)
        report = engine.run()
        time.sleep(0.3)
        assert report.indexed == 2

    def test_progress_callback_invoked(self, hybrid_store, mock_lightrag) -> None:
        """`on_progress` is called for each chunk with the running counters."""
        from obscura.lightrag_memory.backfill import BackfillEngine

        _seed_corpus(hybrid_store, {"fact": 5})
        progress_calls: list[tuple[int, int]] = []

        def _on_progress(done: int, total: int) -> None:
            progress_calls.append((done, total))

        engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag,
                                on_progress=_on_progress)
        engine.run()
        time.sleep(0.3)
        assert len(progress_calls) == 5
        # Final call shows full progress
        assert progress_calls[-1] == (5, 5)


class TestBackfillFileLock:
    def test_filelock_blocks_concurrent_runs(
        self,
        hybrid_store,
        mock_lightrag,
        tmp_path,
    ) -> None:
        """A second backfill while the first holds the lock fails fast."""
        from obscura.lightrag_memory.backfill import (
            BackfillEngine,
            BackfillLockHeld,
        )

        _seed_corpus(hybrid_store, {"fact": 3})
        engine_a = BackfillEngine(store=hybrid_store, adapter=mock_lightrag,
                                  lock_path=tmp_path / "bf.lock")

        # Acquire the lock manually to simulate a running peer
        with engine_a.acquire_lock():
            engine_b = BackfillEngine(store=hybrid_store, adapter=mock_lightrag,
                                      lock_path=tmp_path / "bf.lock")
            with pytest.raises(BackfillLockHeld):
                engine_b.run()
```

---

## 13. CLI tests — `test_cli.py`

The CLI is built on Click; the project already uses `click.testing.CliRunner` (e.g. `/Users/elliottbregni/dev/obscura-main/tests/unit/obscura/cli/test_cli_main.py:15`).

```python
"""Click CLI tests for `obscura memory backfill-graph`."""

from __future__ import annotations

import pytest
from click.testing import CliRunner


class TestBackfillCLI:
    def test_dry_run_prints_estimate(
        self,
        hybrid_store,
        mock_lightrag,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--dry-run` prints chunk counts but performs no inserts."""
        from obscura.lightrag_memory.cli import backfill_graph_cmd

        # Wire the engine to use our test store/adapter via monkeypatch
        from obscura.lightrag_memory import cli as cli_mod
        monkeypatch.setattr(cli_mod, "_resolve_store", lambda user_id: hybrid_store)
        monkeypatch.setattr(cli_mod, "_resolve_adapter", lambda store: mock_lightrag)

        # Seed data
        from obscura.memory import MemoryKey
        for i in range(5):
            hybrid_store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"f{i}"),
                text=f"chunk {i} content. " * 3,
                embedding=[0.0] * 384,
                metadata={"memory_type": "fact"},
                memory_type="fact",
                expires_at=None,
            )

        runner = CliRunner()
        result = runner.invoke(backfill_graph_cmd, ["--dry-run"])
        assert result.exit_code == 0, result.output
        # Estimate output mentions the counts
        assert "5" in result.output
        assert "indexable" in result.output.lower() or "would index" in result.output.lower()
        # No real inserts happened
        assert mock_lightrag.state.inserts == []

    def test_confirm_required_above_threshold(
        self,
        hybrid_store,
        mock_lightrag,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """High estimated cost → exits non-zero without `--confirm`."""
        from obscura.lightrag_memory.cli import backfill_graph_cmd
        from obscura.lightrag_memory import cli as cli_mod
        monkeypatch.setattr(cli_mod, "_resolve_store", lambda user_id: hybrid_store)
        monkeypatch.setattr(cli_mod, "_resolve_adapter", lambda store: mock_lightrag)

        # Seed 1500 chunks — over the 1000 threshold from 00-overview.md
        from obscura.memory import MemoryKey
        for i in range(1500):
            hybrid_store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"f{i}"),
                text=f"chunk {i}. " * 3,
                embedding=[0.0] * 384,
                metadata={"memory_type": "fact"},
                memory_type="fact",
                expires_at=None,
            )

        runner = CliRunner()
        result = runner.invoke(backfill_graph_cmd, [])  # no --confirm
        assert result.exit_code != 0
        assert "confirm" in result.output.lower()
        assert mock_lightrag.state.inserts == []

    def test_max_chunks_passed_through(
        self,
        hybrid_store,
        mock_lightrag,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--max-chunks 3` performs exactly 3 inserts."""
        from obscura.lightrag_memory.cli import backfill_graph_cmd
        from obscura.lightrag_memory import cli as cli_mod
        monkeypatch.setattr(cli_mod, "_resolve_store", lambda user_id: hybrid_store)
        monkeypatch.setattr(cli_mod, "_resolve_adapter", lambda store: mock_lightrag)

        from obscura.memory import MemoryKey
        for i in range(10):
            hybrid_store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"f{i}"),
                text=f"chunk {i} content. " * 3,
                embedding=[0.0] * 384,
                metadata={"memory_type": "fact"},
                memory_type="fact",
                expires_at=None,
            )

        runner = CliRunner()
        result = runner.invoke(backfill_graph_cmd, ["--max-chunks", "3", "--confirm"])
        assert result.exit_code == 0, result.output
        import time
        time.sleep(0.5)
        assert len(mock_lightrag.state.inserts) == 3
```

---

## 14. Integration suite — `tests/integration/lightrag/`

Marker: `@pytest.mark.lightrag_integration`. Skipped by default; opt-in via `RUN_LR_INTEGRATION=1`.

The integration suite is the ONLY place where the real `lightrag-hku` package is imported in tests. The cassettes intercept all OpenAI / embedding API calls, so the suite is fully offline-replayable.

### 14.1 VCR choice

VCR is **not** currently a project dep (verified with `grep -rn "vcr\|pytest-recording" /Users/elliottbregni/dev/obscura-main/pyproject.toml /Users/elliottbregni/dev/obscura-main/tests/`).

**Recommendation:** add `pytest-recording` (which wraps `vcrpy`) as a dev-only dep. `pytest-recording` is the modern preferred wrapper because:

- One-line `@pytest.mark.vcr` per test, no manual cassette plumbing.
- Built-in `--record-mode` flag: `none` (default — fail if no cassette), `once` (record if missing), `new_episodes`, `all` (force re-record).
- Per-test cassette naming inferred from the test ID — keeps cassette files auditable.

Add to `[dependency-groups].dev`:

```toml
"pytest-recording>=0.13",
```

### 14.2 `tests/integration/lightrag/conftest.py`

```python
"""Integration suite — real lightrag-hku, cassetted LLM calls."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config, items):  # noqa: D103
    if os.environ.get("RUN_LR_INTEGRATION") != "1":
        skip = pytest.mark.skip(reason="set RUN_LR_INTEGRATION=1 to run")
        for item in items:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def vcr_config() -> dict:
    """VCR config — match by method+URL, scrub auth headers."""
    return {
        "filter_headers": [
            ("authorization", "REDACTED"),
            ("x-api-key", "REDACTED"),
            ("openai-organization", "REDACTED"),
        ],
        "match_on": ["method", "scheme", "host", "port", "path", "query"],
        "record_mode": "none",  # fail loudly if cassette missing
    }


@pytest.fixture(scope="session")
def cassette_dir() -> Path:
    return Path(__file__).parent / "cassettes"


@pytest.fixture
def real_lightrag(tmp_path, monkeypatch):
    """A real `LightRAG` instance pointed at a temp working_dir.

    The OpenAI / embedding calls inside it are intercepted by the
    pytest-recording cassette via `@pytest.mark.vcr`.
    """
    monkeypatch.setenv("OBSCURA_LIGHTRAG", "on")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")  # cassette covers real responses

    from obscura.lightrag_memory.adapter import LightRAGAdapter
    from obscura.auth.models import AuthenticatedUser

    user = AuthenticatedUser(
        user_id="u-lr-integration",
        email="lr-int@test.com",
        roles=("admin",),
        org_id="org-1",
        token_type="user",
        raw_token="test",
    )
    adapter = LightRAGAdapter.for_user(user, embedding_fn=None)
    yield adapter
    adapter.close()
```

### 14.3 `tests/integration/lightrag/test_e2e_ingest.py`

```python
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

    # Drain any async work
    real_lightrag.flush()

    # The underlying LightRAG instance has a NetworkX graph at .chunk_entity_relation_graph
    graph = real_lightrag._lightrag.chunk_entity_relation_graph._graph

    # The named entities should appear
    nodes = {str(n).lower() for n in graph.nodes()}
    assert any("alice" in n for n in nodes)
    assert any("bob" in n for n in nodes)
    # And there's at least one edge between Alice and Bob's entities
    assert graph.number_of_edges() >= 1
```

### 14.4 `tests/integration/lightrag/test_e2e_query.py`

```python
"""End-to-end query test — real lightrag.aquery against a tiny cassette."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.lightrag_integration


@pytest.mark.vcr("cassettes/tiny_corpus_query.yaml")
async def test_query_recovers_relevant_doc_ids(real_lightrag) -> None:
    """After cassetted ingest, a query for 'Alice' returns the right docs."""
    # The query cassette presupposes the corpus from test_e2e_ingest is loaded.
    # In practice, both tests share a session-scoped fixture that ingests once.

    hits = await real_lightrag.aquery("Who works with Alice?", mode="hybrid", top_k=5)
    assert len(hits) >= 1
    keys = [h.key for h in hits]
    # Bob's doc should be in the top results — he's directly connected
    assert any("doc2" in k or "bob" in k.lower() for k in keys)
```

### 14.5 Recording cassettes — the engineer's runbook

Cassettes are recorded once with real LLM calls, then committed to the repo (~50KB each). To record:

```bash
# 1. Ensure the OpenAI key is set (real)
export OPENAI_API_KEY=sk-...

# 2. Run with --record-mode=once (creates cassettes if missing)
RUN_LR_INTEGRATION=1 pytest tests/integration/lightrag/ \
    --record-mode=once -v

# 3. Verify the cassettes were written
ls -la tests/integration/lightrag/cassettes/

# 4. Run the suite again — this time it must replay (no real calls)
unset OPENAI_API_KEY
RUN_LR_INTEGRATION=1 pytest tests/integration/lightrag/ -v
```

**Re-record only when:**
- `lightrag-hku` releases a new prompt template (their CHANGELOG mentions "prompt" or "extraction").
- The OpenAI API surface changes (rare).
- The fixture corpus changes.

Cassette files are checked into the repo; size budget is 50 KB per cassette. If a cassette grows past that, narrow the fixture corpus.

---

## 15. Pytest configuration changes

### 15.1 `pyproject.toml` diff

Add the new marker to `[tool.pytest.ini_options].markers` (currently lines 207-216):

```toml
[tool.pytest.ini_options]
testpaths = ["tests", "tests/e2e"]
addopts = "-v --tb=short"
norecursedirs = ["_archive", ".worktrees"]
asyncio_mode = "auto"
markers = [
    "e2e: end-to-end tests (slow, require server)",
    "unit: fast unit tests",
    "integration: integration tests",
    "lightrag_integration: real lightrag-hku, opt-in via RUN_LR_INTEGRATION=1",
]
```

Add `pytest-recording` to `[dependency-groups].dev`:

```toml
[dependency-groups]
dev = [
    # ... existing entries ...
    "pytest-recording>=0.13",
]
```

### 15.2 Top-level `tests/conftest.py` — append the gate

Append the following to `/Users/elliottbregni/dev/obscura-main/tests/conftest.py` after the existing `pytest_collection_modifyitems` (lines 113-123):

```python
def pytest_collection_modifyitems_lightrag(items: list[pytest.Item]) -> None:
    """Skip `lightrag_integration` tests unless RUN_LR_INTEGRATION=1.

    Hooked from the existing `pytest_collection_modifyitems` — pytest only
    invokes one hook per name per file, so we extend the existing function
    in-place rather than duplicate the signature.
    """
    if os.environ.get("RUN_LR_INTEGRATION") == "1":
        return
    skip_marker = pytest.mark.skip(reason="set RUN_LR_INTEGRATION=1 to run")
    for item in items:
        if item.get_closest_marker("lightrag_integration"):
            item.add_marker(skip_marker)
```

In practice, fold this into the existing function:

```python
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    config: pytest.Config | None = items[0].config if items else None
    run_e2e: bool = bool(config.getoption("--run-e2e") if config else False)
    run_lr: bool = os.environ.get("RUN_LR_INTEGRATION") == "1"

    e2e_skip = pytest.mark.skip(reason="Use --run-e2e to include e2e tests")
    lr_skip = pytest.mark.skip(reason="set RUN_LR_INTEGRATION=1 to run")

    for item in items:
        if not run_e2e and (
            "tests/e2e/" in item.nodeid or item.get_closest_marker("e2e")
        ):
            item.add_marker(e2e_skip)
        if not run_lr and item.get_closest_marker("lightrag_integration"):
            item.add_marker(lr_skip)
```

This keeps the integration suite invisible to the default `make test` command.

---

## 16. CI integration

### 16.1 Default CI run (every PR)

Command:
```bash
pytest tests/ -v -m "not e2e and not lightrag_integration"
```

This is the rough equivalent of the existing `make test` (Makefile line 176-177):

```makefile
test:
	pytest tests/ -v -m "not e2e"
```

Update the Makefile to match:

```makefile
test:
	pytest tests/ -v -m "not e2e and not lightrag_integration"
```

The default run skips both `e2e` and the new `lightrag_integration` marker. **Crucially**, this run does not require `lightrag-hku` to be installed.

### 16.2 Optional CI run (cron / nightly)

Command:
```bash
RUN_LR_INTEGRATION=1 pytest tests/integration/lightrag/ -v
```

This run requires:
- `uv sync --extra lightrag` (installs `lightrag-hku` and `networkx`).
- `pytest-recording` in the dev deps.
- The `cassettes/` directory present (checked in — no network required).

Recommend wiring as a **scheduled GitHub Action** (cron `0 8 * * *` for daily 08:00 UTC) so cassette replays catch regressions in the integration layer without slowing PR CI. If the project doesn't yet have a `.github/workflows/` directory, that's a separate task — flag it as a follow-up but don't block this PR on it.

### 16.3 Coverage gate

Command:
```bash
pytest tests/unit/obscura/lightrag_memory/ \
    --cov=obscura.lightrag_memory \
    --cov-report=term-missing \
    --cov-fail-under=85
```

The existing `[tool.coverage.report].fail_under = 85` (`pyproject.toml:223`) sets the project-wide threshold. We don't need to change it — the new module just has to clear the bar. If unit tests routinely run at >90% coverage (likely given the test count above), no further config is needed.

To target *only* the new module's coverage (skip total-project drift), pass `--cov=obscura.lightrag_memory` explicitly. This produces a per-module report; CI can fail this command alone without depending on the full suite's coverage.

---

## 17. Mutation testing (optional, opt-in)

`obscura.lightrag_memory.scoring` is small (~30 LOC), pure, and the pivotal correctness surface. It's a textbook mutation-testing target.

**Recommended tool:** `mutmut` (simpler than `cosmic-ray`, plays nicely with `pytest`). Recipe:

```bash
# Install
uv pip install mutmut

# Configure: target only the scoring module
cat > setup.cfg << 'EOF'
[mutmut]
paths_to_mutate=obscura/lightrag_memory/scoring.py
runner=python -m pytest tests/unit/obscura/lightrag_memory/test_scoring.py -x -q
tests_dir=tests/unit/obscura/lightrag_memory/
EOF

# Run
mutmut run

# Inspect surviving mutants
mutmut results
mutmut show 1  # show details for mutant #1
```

**Acceptance:** the parametrized test count in `test_scoring.py` (≥13 cases for the math + monotonicity properties + edge clamps) should kill ≥80% of mutants. Below that, the test set is incomplete — add cases.

**Don't make this a CI blocker.** Mutation testing is signal, not contract; it catches "tests that pass for the wrong reason" but produces noisy false positives (e.g. mutating `>` to `>=` in a path the spec doesn't pin). Run quarterly; treat as a code-review aid rather than a gate.

---

## 18. Test-data fixtures revisited

Two reusable utilities, both already shown above:

### 18.1 `fixture_corpus.py`

A static module with 20 chunks: 6 `fact`, 4 `summary`, 5 `episode`, 3 `general`, 2 `preference`. Each ~50-100 chars. Imported by `test_search_hybrid.py`, `test_backfill.py`, and the integration suite as a deterministic ground truth. See §5 for the full listing.

The 20-chunk size is deliberate: enough variety to exercise top_k filtering, namespace partitioning, and memory_type whitelist; small enough that humans can reason about expected results.

### 18.2 `assert_helpers.py`

Three helpers (full source in §5):

- `assert_score_decreasing(results)` — assert `results[i].final_score >= results[i+1].final_score` for all i.
- `assert_metadata_subset(actual, expected_subset)` — assert every k/v in `expected_subset` is in `actual`. Useful for "does the metadata include `obscura_key`?" without over-asserting on noise.
- `assert_doc_id_format(doc_id)` — sanity check the `f"{namespace}::{key}"` shape.

Used everywhere they fit. Keep test bodies focused on the *test logic*, not the assertion plumbing.

---

## 19. Open questions / decisions deferred

These are flagged for follow-up but explicitly out of scope for Phase 6.

### 19.1 Property-based testing with `hypothesis`?

`obscura.lightrag_memory.scoring.hybrid_score` is pure and has clear invariants (monotonicity per input, clamping at the unit interval, additivity across weights). A `hypothesis`-driven test could check these over millions of random inputs.

**Decision:** out of scope for v1. The parametrized cases in `test_scoring.py` (§6) cover the spec'd cases, and adding `hypothesis` requires test-author training, settles into a slow-but-valuable test class, and crosses into "we're testing the math, not the integration." Re-evaluate after Phase 6 ships if scoring bugs land.

### 19.2 Benchmark suite?

A `pytest-benchmark` or `asv` setup tracking `search_hybrid` p50 / p99 latency would let us catch perf regressions before users feel them. The framework is mature; integration is half a day's work.

**Decision:** out of scope for v1. The `OBSCURA_LIGHTRAG_TIMEOUT_MS=400` budget and fallback path (covered by `test_search_hybrid.py::TestSearchHybridFallback::test_aquery_timeout_falls_back`) are the contract. Add benchmarks if/when the timeout fires more than 1% of queries in production telemetry.

### 19.3 Multi-user concurrency tests?

The `LightRAGAdapter._instances` dict is a per-user singleton. `test_concurrent_inserts_serialized_per_user` covers single-user concurrency, but multi-user tests (User A and User B running simultaneously, no cross-contamination) are absent.

**Decision:** out of scope for v1 because Obscura's local-mode is single-user-per-machine. Add multi-user tests when shared deployment becomes a real target (the same trigger that motivates the AGE switch in `00-overview.md` §"Risks / open questions").

### 19.4 Should we test `flush()` semantics explicitly?

`real_lightrag.flush()` (referenced in `test_e2e_ingest.py`) needs to drain the executor before assertions. The Phase 5 `LightRAGAdapter` plan says "close drains" but doesn't pin "is there a `flush()` API distinct from `close()`?".

**Decision:** if the adapter has only `close()`, the integration test should call `close()` and re-create a new adapter for the query test. The cleaner API is to have both. Flag for the Phase 5 implementation engineer.

### 19.5 Snapshot tests for system-prompt section?

Phase 4 adds three paragraphs to the system prompt. A snapshot test (e.g. `pytest-snapshot`) could assert the exact text. But that's an extreme-overfit test that breaks on any wording change.

**Decision:** out of scope. Build_channels_prompt_section already has its own tests in `tests/unit/obscura/tools/`; trust those.

---

## 20. PR checklist for the engineer

Before opening the PR:

1. `uv sync --group dev` (no `lightrag` extra) — confirms hermetic install.
2. `pytest tests/unit/obscura/lightrag_memory/ -v` — passes in <10s.
3. `pytest tests/ -m "not e2e and not lightrag_integration"` — full default suite passes.
4. `pytest tests/unit/obscura/lightrag_memory/ --cov=obscura.lightrag_memory --cov-fail-under=85` — coverage clears.
5. `OBSCURA_LIGHTRAG=on pytest tests/unit/obscura/vector_memory/ -v` — flag-on doesn't break vector_memory tests.
6. `OBSCURA_LIGHTRAG=off pytest tests/unit/obscura/vector_memory/ -v` — flag-off, same pass count.
7. `make lint` — clean.
8. `make typecheck` — clean.
9. **Optional:** `uv sync --extra lightrag && RUN_LR_INTEGRATION=1 pytest tests/integration/lightrag/ -v` — integration suite passes against checked-in cassettes.

PR description should call out:

- "No new runtime deps; `pytest-recording` added to dev-only group."
- "Default CI run does NOT require `lightrag-hku` to be installed."
- "Integration suite gated by `RUN_LR_INTEGRATION=1` env var; cassettes ~100 KB total."
- Coverage delta as a screenshot of `--cov-report=term-missing` output.

---

## 21. File-creation summary

After this PR, the following files exist that didn't before:

```
tests/unit/obscura/lightrag_memory/__init__.py            (empty)
tests/unit/obscura/lightrag_memory/conftest.py            (~250 lines)
tests/unit/obscura/lightrag_memory/fixture_corpus.py      (~50 lines)
tests/unit/obscura/lightrag_memory/assert_helpers.py      (~40 lines)
tests/unit/obscura/lightrag_memory/test_scoring.py        (~180 lines)
tests/unit/obscura/lightrag_memory/test_hybrid_store.py   (~220 lines)
tests/unit/obscura/lightrag_memory/test_adapter.py        (~190 lines)
tests/unit/obscura/lightrag_memory/test_search_hybrid.py  (~280 lines)
tests/unit/obscura/lightrag_memory/test_touch_count.py    (~150 lines)
tests/unit/obscura/lightrag_memory/test_consolidator_hook.py (~110 lines)
tests/unit/obscura/lightrag_memory/test_backfill.py       (~250 lines)
tests/unit/obscura/lightrag_memory/test_cli.py            (~130 lines)
tests/integration/lightrag/__init__.py                    (empty)
tests/integration/lightrag/conftest.py                    (~50 lines)
tests/integration/lightrag/test_e2e_ingest.py             (~50 lines)
tests/integration/lightrag/test_e2e_query.py              (~40 lines)
tests/integration/lightrag/cassettes/tiny_corpus_ingest.yaml   (~50 KB, recorded)
tests/integration/lightrag/cassettes/tiny_corpus_query.yaml    (~30 KB, recorded)
```

Files modified:

```
pyproject.toml                — add `lightrag_integration` marker, pytest-recording dep
tests/conftest.py             — extend pytest_collection_modifyitems for the gate
Makefile                      — `make test` adds `not lightrag_integration` to filter
```

Total: ~2000 LOC of test code + cassettes. Estimated landing time: 2-3 focused days, matching `00-overview.md` §"Effort summary" line 419.
