"""Unified error categorisation enum.

Three sub-domains formerly defined disjoint `ErrorCategory` enums in
`core/agent_loop.py`, `core/kairos/errors.py`, and `core/supervisor/errors.py`.
Members are merged here with sub-domain prefixes so each domain's semantics
remain unambiguous in stack traces, logs, and metric labels.

Wire format: each value matches the original per-domain string byte-for-byte
(e.g. `AGENT_TRANSIENT = "transient"`, `KAIROS_GOAL = "goal"`,
`SUPERVISOR_TOOL_TRANSIENT = "tool_transient"`). No collisions exist across
the 18 original values, so no value needed re-prefixing — supervisor rows
already in `events.db` continue to round-trip unchanged.

The legacy `ErrorCategory` symbols at the three old import paths remain as
small per-domain `StrEnum` shims with byte-identical values, so existing
call sites and existing persisted data keep working without modification.
New code should import from here for cross-domain awareness.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    """Cross-domain error categories used by retry policies and metrics."""

    AGENT_TRANSIENT = "transient"
    AGENT_MODEL_ERROR = "model_error"
    AGENT_FATAL = "fatal"

    KAIROS_GOAL = "goal"
    KAIROS_PLAN = "plan"
    KAIROS_TASK = "task"
    KAIROS_INTERVENTION = "intervention"
    KAIROS_BUDGET = "budget"
    KAIROS_RUNTIME = "runtime"

    SUPERVISOR_TOOL_TRANSIENT = "tool_transient"
    SUPERVISOR_TOOL_PERMANENT = "tool_permanent"
    SUPERVISOR_MODEL_TRANSIENT = "model_transient"
    SUPERVISOR_MODEL_PERMANENT = "model_permanent"
    SUPERVISOR_LOCK_CONTENTION = "lock_contention"
    SUPERVISOR_LOCK_EXPIRED = "lock_expired"
    SUPERVISOR_STATE_VIOLATION = "state_violation"
    SUPERVISOR_MEMORY_ERROR = "memory_error"
    SUPERVISOR_TIMEOUT = "timeout"
