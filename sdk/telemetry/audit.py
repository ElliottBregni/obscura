# pyright: reportMissingImports=false
"""
sdk.telemetry.audit — Compliance audit logger.

Writes append-only JSONL audit events for every significant action:
agent sends, tool executions, sync triggers, session lifecycle. Each
event links to the OTel trace for full distributed tracing.

Usage::

    from sdk.telemetry.audit import AuditEvent, emit_audit_event

    emit_audit_event(AuditEvent(
        event_type="agent.send",
        user_id="u_123",
        user_email="dev@example.com",
        resource="backend:copilot",
        action="execute",
        outcome="success",
        details={"prompt_len": 42},
    ))
"""

from __future__ import annotations

import importlib
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Audit event dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuditEvent:
    """Immutable record of an auditable action.

    Fields populated automatically if not provided:
    - ``timestamp``: ISO 8601 UTC
    - ``trace_id``: from current OTel context
    """

    event_type: str       # "agent.send", "tool.execute", "sync.trigger", "session.create"
    user_id: str          # From AuthenticatedUser (or "system" for CLI)
    user_email: str       # From AuthenticatedUser (or "system" for CLI)
    resource: str         # "backend:copilot", "tool:read_file", "sync:vault"
    action: str           # "read", "write", "execute", "delete"
    outcome: str          # "success", "denied", "error"
    details: dict[str, object] = field(default_factory=lambda: {})
    timestamp: str = ""
    trace_id: str = ""

    def __post_init__(self) -> None:
        # Frozen dataclass workaround for defaults
        if not self.timestamp:
            object.__setattr__(
                self, "timestamp",
                datetime.now(timezone.utc).isoformat(),
            )
        if not self.trace_id:
            object.__setattr__(self, "trace_id", _current_trace_id())


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------

_audit_log_path: Path | None = None
_file_lock = threading.Lock()


def set_audit_log_path(path: Path | str) -> None:
    """Set the path for the append-only audit log file."""
    global _audit_log_path
    _audit_log_path = Path(path)


def get_audit_log_path() -> Path:
    """Return the current audit log path, defaulting to ``./audit.jsonl``."""
    if _audit_log_path is not None:
        return _audit_log_path
    return Path(os.environ.get("OBSCURA_AUDIT_LOG", "audit.jsonl"))


def emit_audit_event(event: AuditEvent) -> None:
    """Write audit event to JSONL file and OTel log exporter.

    The file write is thread-safe and append-only.
    """
    record = asdict(event)

    # 1. Write to append-only JSONL file
    _write_to_file(record)

    # 2. Emit to OTel log exporter (if available)
    _emit_to_otel(record)

    # 3. Also emit to structlog for unified logging
    _emit_to_structlog(event)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _write_to_file(record: dict[str, Any]) -> None:
    """Append a JSON line to the audit log file."""
    path = get_audit_log_path()
    with _file_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str, separators=(",", ":")) + "\n")


def _emit_to_otel(record: dict[str, Any]) -> None:
    """Emit audit event as an OTel log record."""
    try:
        trace = importlib.import_module("opentelemetry.trace")
        span = trace.get_current_span()
        if span and span.is_recording():
            span.add_event(
                "audit",
                attributes={
                    "audit.event_type": record.get("event_type", ""),
                    "audit.user_id": record.get("user_id", ""),
                    "audit.resource": record.get("resource", ""),
                    "audit.action": record.get("action", ""),
                    "audit.outcome": record.get("outcome", ""),
                },
            )
    except ImportError:
        pass


def _emit_to_structlog(event: AuditEvent) -> None:
    """Log the audit event via structlog."""
    try:
        from sdk.telemetry.logging import get_logger

        logger = get_logger("obscura.audit")
        logger.info(
            "audit.event",
            event_type=event.event_type,
            user_id=event.user_id,
            resource=event.resource,
            action=event.action,
            outcome=event.outcome,
            trace_id=event.trace_id,
        )
    except Exception:
        pass


def _current_trace_id() -> str:
    """Extract the current trace ID from OTel context, or return empty string."""
    try:
        trace = importlib.import_module("opentelemetry.trace")
        span = trace.get_current_span()
        ctx = getattr(span, "get_span_context", lambda: None)()
        if ctx and getattr(ctx, "trace_id", 0):
            return format(ctx.trace_id, "032x")
    except (ImportError, AttributeError):
        pass
    return ""
