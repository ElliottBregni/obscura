# pyright: reportMissingImports=false
"""obscura.telemetry.logging — Structured logging via structlog.

Configures structlog with a JSON renderer (production) or console renderer
(development). Automatically binds ``trace_id`` and ``span_id`` from the
current OTel context to every log entry.

Usage::

    from obscura.telemetry.logging import get_logger

    logger = get_logger(__name__)
    logger.info("request.started", backend="copilot", prompt_len=42)
"""

from __future__ import annotations

import importlib
import logging
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from obscura.core.config import ObscuraConfig

_configured = False


def configure_logging(config: ObscuraConfig) -> None:
    """Configure structlog processors and renderer.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _configured
    if _configured:
        return

    try:
        import structlog
    except ImportError:
        # structlog not installed — fall back to stdlib logging
        logging.basicConfig(
            level=getattr(logging, config.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            stream=sys.stderr,
        )
        _configured = True
        return

    processors: list[Callable[..., Any]] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        _safe_add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        _add_otel_context,
        # Redaction runs after all other attributes are merged so we scrub
        # whatever the call site, context vars, and exception frames added.
        # Must run before StackInfoRenderer/format_exc_info so we catch
        # secrets that show up in traceback strings too.
        _redact_event_dict,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if config.log_format == "text":
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Also configure stdlib logging to route through structlog
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        stream=sys.stderr,
    )

    _configured = True


def get_logger(name: str) -> Any:
    """Return a structlog logger, or a stdlib logger if structlog is unavailable."""
    try:
        import structlog

        return structlog.get_logger(name)
    except ImportError:
        return logging.getLogger(name)


def _add_otel_context(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Structlog processor that injects trace_id and span_id from OTel context."""
    try:
        trace = importlib.import_module("opentelemetry.trace")
        span = trace.get_current_span()
        ctx = getattr(span, "get_span_context", lambda: None)()
        if ctx and getattr(ctx, "trace_id", 0):
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
            event_dict["span_id"] = format(ctx.span_id, "016x")
    except (ImportError, AttributeError):
        pass

    return event_dict


def _redact_event_dict(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Structlog processor that scrubs known secret patterns from every log.

    Runs on every log event regardless of call site so we catch secrets
    that reached the logger from exception frames, context vars, or the
    library layer. See obscura.core.redaction for the pattern library.
    """
    _ = logger
    _ = method_name
    try:
        from obscura.core.redaction import redact_mapping

        return redact_mapping(event_dict)
    except Exception:
        # Never let redaction failure drop a log line. Log loss would be
        # a worse outcome than an unredacted record.
        return event_dict


def _safe_add_logger_name(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Add logger name if available (PrintLogger has no `name`)."""
    _ = method_name
    name = getattr(logger, "name", None)
    if isinstance(name, str) and name:
        event_dict["logger"] = name
    return event_dict


def _reset() -> None:
    """Reset configuration state (testing only)."""
    global _configured
    _configured = False


_RESET_HOOK = _reset  # keep referenced for tests
