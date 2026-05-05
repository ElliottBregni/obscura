"""Standalone healthcheck for the configured vector backend.

Call :func:`vector_healthcheck` to ping the backend without scoping it
to a user. Used by the ``/doctor`` slash command, the ``vector_health``
agent tool, and the boot diagnostics. Never raises — returns a
structured dict so callers can render the result themselves.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from obscura.data.vector_memory.errors import (
    VectorBackendUnavailable,
    VectorMemoryDisabled,
    VectorMemoryError,
)
from obscura.data.vector_memory.factory import (
    get_vector_memory_repo,
    is_vector_memory_enabled,
    resolve_vector_backend,
)

logger = logging.getLogger(__name__)


# Probe with a tiny zero-vector and tiny dim so any reachable backend
# answers; the actual dim doesn't have to match production usage —
# this is a connectivity test, not a vector-quality check.
_PROBE_DIM = 4
_PROBE_USER = "__healthcheck__"


def vector_healthcheck() -> dict[str, Any]:
    """Return a status dict for the configured vector backend.

    Keys:
        ok (bool): True iff the backend initialised AND responded to a
            stats() call within ``timeout_s``.
        backend (str): Resolved backend name.
        enabled (bool): False when ``OBSCURA_VECTOR_MEMORY=off``.
        latency_ms (float | None): Round-trip time of the probe.
        error (str | None): Human-readable failure cause when ok=False.
    """
    if not is_vector_memory_enabled():
        return {
            "ok": False,
            "backend": None,
            "enabled": False,
            "latency_ms": None,
            "error": "OBSCURA_VECTOR_MEMORY=off",
        }
    try:
        backend_name = resolve_vector_backend()
    except VectorMemoryError as exc:
        logger.debug("vector backend resolution failed", exc_info=True)
        return {
            "ok": False,
            "backend": None,
            "enabled": True,
            "latency_ms": None,
            "error": str(exc),
        }

    repo = None
    try:
        t0 = time.perf_counter()
        repo = get_vector_memory_repo(
            user_id=_PROBE_USER,
            embedding_dim=_PROBE_DIM,
        )
        ok = repo.healthcheck()
        latency_ms = (time.perf_counter() - t0) * 1000.0
    except VectorMemoryDisabled as exc:
        logger.debug("vector memory disabled mid-probe", exc_info=True)
        return {
            "ok": False,
            "backend": backend_name,
            "enabled": False,
            "latency_ms": None,
            "error": str(exc),
        }
    except VectorBackendUnavailable as exc:
        logger.debug("vector backend unavailable", exc_info=True)
        return {
            "ok": False,
            "backend": backend_name,
            "enabled": True,
            "latency_ms": None,
            "error": str(exc),
        }
    except Exception as exc:
        logger.debug("unexpected vector healthcheck error", exc_info=True)
        return {
            "ok": False,
            "backend": backend_name,
            "enabled": True,
            "latency_ms": None,
            "error": f"unexpected: {exc}",
        }
    finally:
        if repo is not None:
            repo.close()
    return {
        "ok": ok,
        "backend": backend_name,
        "enabled": True,
        "latency_ms": round(latency_ms, 2),
        "error": None,
    }
