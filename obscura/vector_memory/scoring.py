"""Hybrid scoring for vector-memory retrieval.

Combines four signals — vector similarity, recency decay, usage
frequency, and (forward-compat) graph relevance — into a single linear
blend.  ``hybrid_score`` is consumed by ``RecencyReranker`` and any
future graph-aware reranker.

The default weights keep vector similarity dominant (0.7) so existing
queries retain their current ranking shape; decay (0.25) and usage
(0.05) layer in as additive nudges, while graph (0.0) is reserved for
a future graph-retrieval layer.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_USAGE_SATURATION_K = 100


@dataclass(frozen=True)
class HybridWeights:
    """Linear-blend weights for the four hybrid-retrieval signals."""

    vector: float = 0.7
    decay: float = 0.25
    usage: float = 0.05
    graph: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (
            ("vector", self.vector),
            ("decay", self.decay),
            ("usage", self.usage),
            ("graph", self.graph),
        ):
            if value < 0:
                msg = f"HybridWeights.{name} must be >= 0, got {value}"
                raise ValueError(msg)


def hybrid_score(
    *,
    vector_sim: float,
    decay_multiplier: float,
    usage_count: int,
    graph_relevance: float = 0.0,
    weights: HybridWeights,
    saturation_k: int = DEFAULT_USAGE_SATURATION_K,
) -> float:
    """Compute the linear-blend hybrid score.

    All four input components are clamped to ``[0, 1]`` defensively.
    ``usage_count`` is normalized via log-saturation so a chunk read 100
    times scores roughly 1.0; reads beyond that have diminishing return.
    """
    v = max(0.0, min(1.0, vector_sim))
    d = max(0.0, min(1.0, decay_multiplier))
    g = max(0.0, min(1.0, graph_relevance))

    if usage_count < 0:
        usage_count = 0
    u_raw = math.log1p(usage_count) / math.log1p(saturation_k)
    u = min(1.0, max(0.0, u_raw))

    return (
        weights.vector * v + weights.decay * d + weights.usage * u + weights.graph * g
    )


def load_hybrid_weights(raw: dict[str, Any] | None = None) -> HybridWeights:
    """Build a :class:`HybridWeights` from the raw config dict.

    *raw* should be the ``[vector_memory.scoring]`` section of config.toml.
    Any missing field falls back to the dataclass default.
    """
    if not raw:
        return HybridWeights()
    return HybridWeights(
        vector=float(raw.get("vector", 0.7)),
        decay=float(raw.get("decay", 0.25)),
        usage=float(raw.get("usage", 0.05)),
        graph=float(raw.get("graph", 0.0)),
    )


def load_hybrid_weights_from_disk() -> HybridWeights:
    """Load weights from ``[vector_memory.scoring]`` in ``~/.obscura/config.toml``.

    Falls back to defaults if the section is absent or unreadable.
    """
    try:
        from obscura.core.config_io import try_load_config

        home_cfg = try_load_config(Path.home() / ".obscura" / "config.toml")
        section = (home_cfg or {}).get("vector_memory", {}).get("scoring")
        return load_hybrid_weights(section)
    except Exception:
        logger.debug(
            "Could not load HybridWeights from disk, using defaults",
            exc_info=True,
        )
        return HybridWeights()
