"""
obscura.core.supervisor.errors — Error taxonomy for the supervisor.

Separates retryable from fatal errors. Each error carries enough context
for the supervisor to decide retry vs fail.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class ErrorCategory(enum.Enum):
    """Broad error categories for supervisor decision-making."""

    TOOL_TRANSIENT = "tool_transient"
    TOOL_PERMANENT = "tool_permanent"
    MODEL_TRANSIENT = "model_transient"
    MODEL_PERMANENT = "model_permanent"
    LOCK_CONTENTION = "lock_contention"
    LOCK_EXPIRED = "lock_expired"
    STATE_VIOLATION = "state_violation"
    MEMORY_ERROR = "memory_error"
    TIMEOUT = "timeout"


# Categories that are safe to retry
RETRYABLE_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {
        ErrorCategory.TOOL_TRANSIENT,
        ErrorCategory.MODEL_TRANSIENT,
        ErrorCategory.LOCK_CONTENTION,
        ErrorCategory.LOCK_EXPIRED,
        ErrorCategory.MEMORY_ERROR,
    }
)


class SupervisorError(Exception):
    """Base error for all supervisor exceptions."""

    def __init__(
        self,
        message: str,
        category: ErrorCategory = ErrorCategory.STATE_VIOLATION,
        *,
        retryable: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.context = context or {}


class StateTransitionError(SupervisorError):
    """Invalid state machine transition."""

    def __init__(self, from_state: str, to_state: str) -> None:
        super().__init__(
            f"Invalid transition: {from_state} -> {to_state}",
            ErrorCategory.STATE_VIOLATION,
            retryable=False,
            context={"from_state": from_state, "to_state": to_state},
        )


class LockAcquisitionError(SupervisorError):
    """Failed to acquire session lock."""

    def __init__(
        self,
        session_id: str,
        *,
        holder_id: str = "",
        timeout: float = 0.0,
    ) -> None:
        super().__init__(
            f"Failed to acquire lock for session {session_id}"
            + (f" (held by {holder_id})" if holder_id else "")
            + (f" after {timeout:.1f}s" if timeout else ""),
            ErrorCategory.LOCK_CONTENTION,
            retryable=True,
            context={
                "session_id": session_id,
                "holder_id": holder_id,
                "timeout": timeout,
            },
        )


class LockExpiredError(SupervisorError):
    """Lock expired during execution (holder took too long)."""

    def __init__(self, session_id: str, holder_id: str) -> None:
        super().__init__(
            f"Lock expired for session {session_id} (holder: {holder_id})",
            ErrorCategory.LOCK_EXPIRED,
            retryable=True,
            context={"session_id": session_id, "holder_id": holder_id},
        )


class RunTimeoutError(SupervisorError):
    """Run exceeded maximum allowed duration."""

    def __init__(self, run_id: str, duration: float, limit: float) -> None:
        super().__init__(
            f"Run {run_id} timed out after {duration:.1f}s (limit: {limit:.1f}s)",
            ErrorCategory.TIMEOUT,
            retryable=False,
            context={
                "run_id": run_id,
                "duration": duration,
                "limit": limit,
            },
        )


class ToolExecutionError(SupervisorError):
    """Tool execution failed."""

    def __init__(
        self,
        tool_name: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        category = (
            ErrorCategory.TOOL_TRANSIENT
            if retryable
            else ErrorCategory.TOOL_PERMANENT
        )
        super().__init__(
            f"Tool '{tool_name}' failed: {message}",
            category,
            retryable=retryable,
            context={"tool_name": tool_name},
        )


class MemoryCommitError(SupervisorError):
    """Memory commit operation failed."""

    def __init__(self, message: str) -> None:
        super().__init__(
            f"Memory commit failed: {message}",
            ErrorCategory.MEMORY_ERROR,
            retryable=True,
        )


class PromptAssemblyError(SupervisorError):
    """Failed to assemble a valid prompt."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            ErrorCategory.STATE_VIOLATION,
            retryable=False,
        )


class DriftDetectedError(SupervisorError):
    """Prompt or tool hash changed mid-run."""

    def __init__(
        self,
        kind: str,
        expected: str,
        actual: str,
    ) -> None:
        super().__init__(
            f"{kind} drift detected: expected {expected[:12]}..., got {actual[:12]}...",
            ErrorCategory.STATE_VIOLATION,
            retryable=False,
            context={"kind": kind, "expected": expected, "actual": actual},
        )
