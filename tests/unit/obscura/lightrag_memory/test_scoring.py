"""Tests for obscura.lightrag_memory.scoring.hybrid_score()."""

from __future__ import annotations

import math

import pytest

from obscura.lightrag_memory.scoring import (
    HybridWeights,
    hybrid_score,
    load_hybrid_weights_from_disk,
)


DEFAULTS = HybridWeights()


def _expected(
    vec: float, graph: float, decay: float, usage: int, w: HybridWeights
) -> float:
    """Reference implementation — computes the canonical expected value."""
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
        (
            1.0,
            0.0,
            0.0,
            0,
            HybridWeights(vector=1.0, graph=0.0, decay=0.0, usage=0.0),
            "all-vector",
        ),
        (
            0.0,
            1.0,
            0.0,
            0,
            HybridWeights(vector=0.0, graph=1.0, decay=0.0, usage=0.0),
            "all-graph",
        ),
        (
            0.0,
            0.0,
            1.0,
            0,
            HybridWeights(vector=0.0, graph=0.0, decay=1.0, usage=0.0),
            "all-decay",
        ),
        (
            0.0,
            0.0,
            0.0,
            1000,
            HybridWeights(vector=0.0, graph=0.0, decay=0.0, usage=1.0),
            "all-usage-saturated",
        ),
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
    common: dict[str, float | int | HybridWeights] = dict(
        graph_relevance=0.5,
        decay_multiplier=0.5,
        usage_count=10,
        weights=HybridWeights(),
    )
    s_lo = hybrid_score(vector_sim=0.2, **common)  # type: ignore[arg-type]
    s_mid = hybrid_score(vector_sim=0.5, **common)  # type: ignore[arg-type]
    s_hi = hybrid_score(vector_sim=0.9, **common)  # type: ignore[arg-type]
    assert s_lo <= s_mid <= s_hi


def test_hybrid_score_monotonic_in_usage() -> None:
    """Raising usage_count never lowers the score (saturating but non-decreasing)."""
    common: dict[str, float | HybridWeights] = dict(
        vector_sim=0.5,
        graph_relevance=0.5,
        decay_multiplier=0.5,
        weights=HybridWeights(usage=0.5, vector=0.5, graph=0.0, decay=0.0),
    )
    s_0 = hybrid_score(usage_count=0, **common)  # type: ignore[arg-type]
    s_10 = hybrid_score(usage_count=10, **common)  # type: ignore[arg-type]
    s_100 = hybrid_score(usage_count=100, **common)  # type: ignore[arg-type]
    s_1000 = hybrid_score(usage_count=1000, **common)  # type: ignore[arg-type]
    assert s_0 <= s_10 <= s_100 <= s_1000


class TestHybridWeightsValidation:
    def test_negative_weight_rejected(self) -> None:
        """Negative weights are nonsensical — constructor must raise."""
        with pytest.raises(ValueError, match="negative"):
            HybridWeights(vector=-0.1)

    def test_non_summing_weights_warn_but_succeed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Weights that don't sum to ~1.0 produce a warning but no error."""
        with caplog.at_level("WARNING"):
            w = HybridWeights(vector=0.9, graph=0.5, decay=0.0, usage=0.0)
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


class TestLoadWeightsFromDisk:
    def test_returns_defaults_when_file_missing(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing config file returns `HybridWeights()` defaults silently."""
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
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
