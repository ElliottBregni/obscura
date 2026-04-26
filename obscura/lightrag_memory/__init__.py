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

import atexit
import logging
import os
import sys
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


def _shutdown_all_adapters() -> None:
    """Close every per-user LightRAGAdapter at process exit.

    Looks the adapter module up via :data:`sys.modules` instead of
    importing it, so we never trigger the lightrag-hku top-level import
    here when the user never opted in to the optional extra.
    """
    adapter_mod = sys.modules.get("obscura.lightrag_memory.adapter")
    if adapter_mod is None:
        return
    close_all = getattr(
        getattr(adapter_mod, "LightRAGAdapter", None), "close_all", None
    )
    if close_all is None:
        return
    try:
        close_all()
    except Exception:
        _log.warning("LightRAG adapter shutdown raised during atexit")


atexit.register(_shutdown_all_adapters)


@lru_cache(maxsize=1)
def _lightrag_enabled() -> bool:
    """Return True iff the LightRAG layer is both *requested* and *available*.

    Precedence (first hit wins):
    1. ``OBSCURA_LIGHTRAG`` environment variable: ``on`` / ``1`` / ``true`` -> True;
       ``off`` / ``0`` / ``false`` -> False.
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
        import lightrag  # noqa: F401  # pyright: ignore[reportMissingImports, reportUnusedImport]
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
