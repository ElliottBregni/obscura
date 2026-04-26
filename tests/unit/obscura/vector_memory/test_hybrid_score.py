"""Tests for hybrid_score and HybridWeights."""

from __future__ import annotations

import math

import pytest

from obscura.vector_memory.scoring import (
    DEFAULT_USAGE_SATURATION_K,
    HybridWeights,
    hybrid_score,
    load_hybrid_weights,
)


class TestHybridWeights:
    def test_defaults(self) -> None:
        w = HybridWeights()
        assert w.vector == 0.7
        assert w.decay == 0.25
        assert w.usage == 0.05
        assert w.graph == 0.0

    def test_negative_weight_raises(self) -> None:
        with pytest.raises(ValueError):
            HybridWeights(vector=-0.1)
        with pytest.raises(ValueError):
            HybridWeights(decay=-1.0)


class TestHybridScore:
    @pytest.mark.parametrize(
        ("vector_sim", "decay", "usage", "graph", "expected"),
        [
            # All zeros
            (0.0, 0.0, 0, 0.0, 0.0),
            # Only vector — default vector weight 0.7
            (1.0, 0.0, 0, 0.0, 0.7),
            # Only decay — default decay weight 0.25
            (0.0, 1.0, 0, 0.0, 0.25),
            # Vector + decay both 1.0
            (1.0, 1.0, 0, 0.0, 0.95),
            # Negative vector clamped to 0
            (-0.5, 1.0, 0, 0.0, 0.25),
            # Vector above 1 clamped to 1
            (5.0, 0.0, 0, 0.0, 0.7),
        ],
    )
    def test_basic_blend(
        self,
        vector_sim: float,
        decay: float,
        usage: int,
        graph: float,
        expected: float,
    ) -> None:
        w = HybridWeights()
        score = hybrid_score(
            vector_sim=vector_sim,
            decay_multiplier=decay,
            usage_count=usage,
            graph_relevance=graph,
            weights=w,
        )
        assert score == pytest.approx(expected, abs=1e-6)

    def test_usage_zero_contributes_nothing(self) -> None:
        w = HybridWeights()
        s = hybrid_score(
            vector_sim=0.0,
            decay_multiplier=0.0,
            usage_count=0,
            weights=w,
        )
        assert s == pytest.approx(0.0, abs=1e-9)

    def test_usage_saturation_at_k(self) -> None:
        """At usage_count == saturation_k, normalized usage is 1.0."""
        w = HybridWeights(vector=0.0, decay=0.0, usage=1.0, graph=0.0)
        s = hybrid_score(
            vector_sim=0.0,
            decay_multiplier=0.0,
            usage_count=DEFAULT_USAGE_SATURATION_K,
            weights=w,
        )
        assert s == pytest.approx(1.0, abs=1e-9)

    def test_usage_saturation_above_k(self) -> None:
        """Beyond saturation_k, usage cap stays at 1.0."""
        w = HybridWeights(vector=0.0, decay=0.0, usage=1.0, graph=0.0)
        s = hybrid_score(
            vector_sim=0.0,
            decay_multiplier=0.0,
            usage_count=1000,
            weights=w,
        )
        assert s == pytest.approx(1.0, abs=1e-9)

    def test_usage_log_curve(self) -> None:
        """log1p curve: 10 reads is roughly half of 100."""
        w = HybridWeights(vector=0.0, decay=0.0, usage=1.0, graph=0.0)
        ten = hybrid_score(
            vector_sim=0.0,
            decay_multiplier=0.0,
            usage_count=10,
            weights=w,
        )
        expected = math.log1p(10) / math.log1p(100)
        assert ten == pytest.approx(expected, abs=1e-9)

    def test_negative_usage_treated_as_zero(self) -> None:
        w = HybridWeights()
        s_neg = hybrid_score(
            vector_sim=0.0,
            decay_multiplier=0.0,
            usage_count=-5,
            weights=w,
        )
        s_zero = hybrid_score(
            vector_sim=0.0,
            decay_multiplier=0.0,
            usage_count=0,
            weights=w,
        )
        assert s_neg == s_zero

    def test_graph_term_default_zero(self) -> None:
        """With default graph weight 0, graph_relevance has no impact."""
        w = HybridWeights()
        with_graph = hybrid_score(
            vector_sim=0.5,
            decay_multiplier=0.5,
            usage_count=10,
            graph_relevance=0.99,
            weights=w,
        )
        no_graph = hybrid_score(
            vector_sim=0.5,
            decay_multiplier=0.5,
            usage_count=10,
            graph_relevance=0.0,
            weights=w,
        )
        assert with_graph == pytest.approx(no_graph)

    def test_graph_term_with_nonzero_weight(self) -> None:
        """When graph weight > 0, graph_relevance contributes."""
        w = HybridWeights(vector=0.5, decay=0.0, usage=0.0, graph=0.5)
        s = hybrid_score(
            vector_sim=1.0,
            decay_multiplier=0.0,
            usage_count=0,
            graph_relevance=1.0,
            weights=w,
        )
        assert s == pytest.approx(1.0, abs=1e-9)

    def test_all_components_max(self) -> None:
        w = HybridWeights()
        s = hybrid_score(
            vector_sim=1.0,
            decay_multiplier=1.0,
            usage_count=DEFAULT_USAGE_SATURATION_K,
            graph_relevance=1.0,
            weights=w,
        )
        # With graph weight = 0, the max is vector + decay + usage = 1.0
        assert s == pytest.approx(1.0, abs=1e-9)

    def test_weights_extreme_only_vector(self) -> None:
        w = HybridWeights(vector=1.0, decay=0.0, usage=0.0, graph=0.0)
        s = hybrid_score(
            vector_sim=0.42,
            decay_multiplier=0.99,
            usage_count=50,
            weights=w,
        )
        assert s == pytest.approx(0.42, abs=1e-9)


class TestLoadHybridWeights:
    def test_none_returns_defaults(self) -> None:
        assert load_hybrid_weights(None) == HybridWeights()

    def test_empty_dict_returns_defaults(self) -> None:
        assert load_hybrid_weights({}) == HybridWeights()

    def test_full_config(self) -> None:
        w = load_hybrid_weights(
            {"vector": 0.5, "decay": 0.3, "usage": 0.1, "graph": 0.1},
        )
        assert w.vector == 0.5
        assert w.decay == 0.3
        assert w.usage == 0.1
        assert w.graph == 0.1

    def test_partial_config_falls_back(self) -> None:
        w = load_hybrid_weights({"vector": 0.6})
        assert w.vector == 0.6
        assert w.decay == 0.25
        assert w.usage == 0.05
        assert w.graph == 0.0
