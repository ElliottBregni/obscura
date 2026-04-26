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
            (home_cfg or {}).get("vector_memory", {}).get("lightrag", {}).get("weights")
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
