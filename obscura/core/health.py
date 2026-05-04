"""obscura.core.health -- Startup health checks for optional dependencies.

Collects warnings about degraded or unavailable optional services
(Qdrant, msgraph, etc.) so they can be surfaced in the CLI banner
instead of buried in debug logs.

Usage::

    from obscura.core.health import collect_startup_health

    checks = collect_startup_health(
        vector_store=vector_store,
        skipped_tools=[("msgraph.mail.list", "No module named 'msal'"), ...],
    )
    # Pass to print_banner() for display
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

from obscura.core.enums.lifecycle import HealthStatus
from obscura.core.models.lifecycle import HealthReport

logger = logging.getLogger(__name__)


# Backwards-compatible alias for the previous dataclass name.
HealthCheck = HealthReport


def _now() -> datetime:
    return datetime.now(UTC)


def _check_vector_memory(vector_store: Any | None) -> HealthReport | None:
    """Check vector memory backend health."""
    requested = os.environ.get("OBSCURA_VECTOR_BACKEND", "qdrant").lower()

    if vector_store is None:
        if os.environ.get("OBSCURA_VECTOR_MEMORY", "").lower() == "off":
            return None  # Intentionally disabled, not a health issue
        return HealthReport(
            name="vector_memory",
            status=HealthStatus.UNAVAILABLE,
            status_changed_at=_now(),
            message="Vector memory failed to initialize",
        )

    backend = getattr(vector_store, "backend", None)
    if backend is None:
        return None

    backend_class = type(backend).__name__
    if requested == "qdrant" and "SQLite" in backend_class:
        return HealthReport(
            name="vector_memory",
            status=HealthStatus.DEGRADED,
            status_changed_at=_now(),
            message="Qdrant unavailable, using SQLite fallback",
        )
    return None  # Healthy — no need to report


def _check_skipped_tools(
    skipped_tools: list[tuple[str, str]],
) -> list[HealthReport]:
    """Group skipped tools by root cause and return health checks."""
    if not skipped_tools:
        return []

    # Group by extracted module name from the error reason
    # e.g. "No module named 'msal'" → "msal"
    groups: dict[str, list[str]] = {}
    for tool_name, handler_ref in skipped_tools:
        # Extract provider prefix from handler_ref (e.g. "obscura.tools.providers.msgraph" → "msgraph")
        provider = (
            handler_ref.rsplit(":", 1)[0].rsplit(".", 1)[-1]
            if handler_ref
            else "unknown"
        )
        groups.setdefault(provider, []).append(tool_name)

    checks: list[HealthReport] = []
    for provider, tools in sorted(groups.items()):
        # Try to find the missing module from the handler ref
        sample_ref = next(
            (ref for _, ref in skipped_tools if provider in ref),
            "",
        )
        # Attempt import to get the actual error message
        reason = _probe_import_error(sample_ref)

        count = len(tools)
        tool_word = "tool" if count == 1 else "tools"
        msg = f"{count} {provider} {tool_word} skipped"
        if reason:
            msg += f" (missing: {reason})"
        checks.append(
            HealthReport(
                name=f"tools:{provider}",
                status=HealthStatus.DEGRADED,
                status_changed_at=_now(),
                message=msg,
            ),
        )
    return checks


def _probe_import_error(handler_ref: str) -> str:
    """Try to import a handler's module and return the missing dependency name."""
    if not handler_ref:
        return ""
    module_path = (
        handler_ref.rsplit(":", 1)[0]
        if ":" in handler_ref
        else handler_ref.rsplit(".", 1)[0]
    )
    try:
        __import__(module_path)
        return ""
    except ImportError as exc:
        # Extract module name from "No module named 'msal'"
        logger.debug("suppressed exception in _probe_import_error", exc_info=True)
        match = re.search(r"No module named '([^']+)'", str(exc))
        return match.group(1) if match else str(exc)
    except Exception:
        logger.debug("suppressed exception in _probe_import_error", exc_info=True)
        return ""


def collect_startup_health(
    *,
    vector_store: Any | None = None,
    skipped_tools: list[tuple[str, str]] | None = None,
) -> list[HealthReport]:
    """Collect startup health checks for optional dependencies.

    Returns only checks with degraded/unavailable status — an empty list
    means everything is healthy.
    """
    checks: list[HealthReport] = []

    vm_check = _check_vector_memory(vector_store)
    if vm_check is not None:
        checks.append(vm_check)

    checks.extend(_check_skipped_tools(skipped_tools or []))

    return checks


__all__ = [
    "HealthCheck",
    "HealthReport",
    "collect_startup_health",
]
