"""Deprecated import location — re-exports :mod:`obscura.runtime.predictive_tools`.

The implementation moved to :mod:`obscura.runtime` as part of the A2 surface
split. This shim keeps existing ``from obscura.core.predictive_tools import …``
callers working; new code should import from the new location directly.
"""

from __future__ import annotations

from obscura.runtime.predictive_tools import (
    CacheEntry,
    PredictiveToolCache,
    ToolPrediction,
    ToolPredictor,
)

__all__ = [
    "CacheEntry",
    "PredictiveToolCache",
    "ToolPrediction",
    "ToolPredictor",
]
