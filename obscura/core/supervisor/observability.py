"""
obscura.core.supervisor.observability — Run observer for logging + drift detection.

Tracks:
- Prompt hash fingerprinting
- Tool registry hash changes
- Policy version hash
- Lock wait times
- Run duration
- Memory commit counts
- Drift detection (prompt/tools changed mid-run)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from obscura.core.supervisor.types import (
    RunContext,
    SupervisorEvent,
    SupervisorEventKind,
    SupervisorState,
)

logger = logging.getLogger(__name__)


@dataclass
class RunMetrics:
    """Accumulated metrics for a single supervised run."""

    run_id: str = ""
    session_id: str = ""

    # Timing
    started_at: float = 0.0
    completed_at: float = 0.0
    lock_wait_ms: float = 0.0
    lock_hold_ms: float = 0.0

    # Context
    prompt_hash: str = ""
    tool_snapshot_hash: str = ""
    policy_hash: str = ""
    agent_version_hash: str = ""

    # Counts
    tool_count: int = 0
    turn_count: int = 0
    heartbeat_count: int = 0
    memory_candidates: int = 0
    memory_committed: int = 0
    memory_deduplicated: int = 0
    memory_gated: int = 0
    hook_fires: int = 0

    # Drift
    drift_detected: bool = False
    drift_details: list[dict[str, str]] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if self.completed_at == 0.0:
            return 0.0
        return (self.completed_at - self.started_at) * 1000

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "duration_ms": round(self.duration_ms, 1),
            "lock_wait_ms": round(self.lock_wait_ms, 1),
            "lock_hold_ms": round(self.lock_hold_ms, 1),
            "prompt_hash": self.prompt_hash[:12] if self.prompt_hash else "",
            "tool_snapshot_hash": self.tool_snapshot_hash[:12] if self.tool_snapshot_hash else "",
            "policy_hash": self.policy_hash[:12] if self.policy_hash else "",
            "agent_version_hash": self.agent_version_hash[:12] if self.agent_version_hash else "",
            "tool_count": self.tool_count,
            "turn_count": self.turn_count,
            "heartbeat_count": self.heartbeat_count,
            "memory_committed": self.memory_committed,
            "memory_deduplicated": self.memory_deduplicated,
            "memory_gated": self.memory_gated,
            "hook_fires": self.hook_fires,
            "drift_detected": self.drift_detected,
        }


class RunObserver:
    """Observes a supervised run, collecting metrics and detecting drift.

    Consumes SupervisorEvents and accumulates metrics for logging
    and monitoring.

    Usage::

        observer = RunObserver(run_id="run-abc", session_id="sess-1")

        # Record context hashes at build time
        observer.record_context(
            prompt_hash="abc...",
            tool_snapshot_hash="def...",
        )

        # Feed events as they happen
        observer.observe(event)

        # Check for drift at any time
        observer.check_drift(current_prompt_hash="abc...")

        # At the end
        metrics = observer.finalize()
        logger.info("Run complete", extra=metrics.to_dict())
    """

    def __init__(self, run_id: str, session_id: str) -> None:
        self._metrics = RunMetrics(run_id=run_id, session_id=session_id)
        self._lock_acquired_at: float = 0.0

    @property
    def metrics(self) -> RunMetrics:
        return self._metrics

    # -- context recording ---------------------------------------------------

    def record_context(
        self,
        *,
        prompt_hash: str = "",
        tool_snapshot_hash: str = "",
        policy_hash: str = "",
        agent_version_hash: str = "",
        tool_count: int = 0,
    ) -> None:
        """Record context hashes at BUILDING_CONTEXT time."""
        self._metrics.prompt_hash = prompt_hash
        self._metrics.tool_snapshot_hash = tool_snapshot_hash
        self._metrics.policy_hash = policy_hash
        self._metrics.agent_version_hash = agent_version_hash
        self._metrics.tool_count = tool_count

    def record_lock_acquired(self, wait_ms: float) -> None:
        """Record lock acquisition timing."""
        self._metrics.lock_wait_ms = wait_ms
        self._lock_acquired_at = time.monotonic()

    def record_lock_released(self) -> None:
        """Record lock release timing."""
        if self._lock_acquired_at > 0:
            self._metrics.lock_hold_ms = (
                time.monotonic() - self._lock_acquired_at
            ) * 1000

    def start(self) -> None:
        """Mark run start."""
        self._metrics.started_at = time.monotonic()

    # -- event observation ---------------------------------------------------

    def observe(self, event: SupervisorEvent) -> None:
        """Observe a supervisor event and update metrics."""
        kind = event.kind

        if kind == SupervisorEventKind.MODEL_TURN_START:
            self._metrics.turn_count += 1

        elif kind == SupervisorEventKind.TOOL_EXECUTION_END:
            pass  # tool count is set at context build time

        elif kind == SupervisorEventKind.HEARTBEAT:
            self._metrics.heartbeat_count += 1

        elif kind == SupervisorEventKind.MEMORY_COMMIT:
            self._metrics.memory_committed += 1

        elif kind == SupervisorEventKind.MEMORY_DEDUPLICATED:
            self._metrics.memory_deduplicated += 1

        elif kind == SupervisorEventKind.MEMORY_GATED:
            self._metrics.memory_gated += 1

        elif kind == SupervisorEventKind.HOOK_FIRED:
            self._metrics.hook_fires += 1

        elif kind == SupervisorEventKind.DRIFT_DETECTED:
            self._metrics.drift_detected = True
            self._metrics.drift_details.append(event.payload)

    # -- drift detection -----------------------------------------------------

    def check_prompt_drift(self, current_hash: str) -> bool:
        """Check if prompt hash has drifted from initial."""
        if not self._metrics.prompt_hash:
            return False
        if current_hash != self._metrics.prompt_hash:
            self._record_drift("prompt", self._metrics.prompt_hash, current_hash)
            return True
        return False

    def check_tool_drift(self, current_hash: str) -> bool:
        """Check if tool snapshot hash has drifted."""
        if not self._metrics.tool_snapshot_hash:
            return False
        if current_hash != self._metrics.tool_snapshot_hash:
            self._record_drift(
                "tool_registry", self._metrics.tool_snapshot_hash, current_hash
            )
            return True
        return False

    def _record_drift(
        self,
        kind: str,
        expected: str,
        actual: str,
    ) -> None:
        """Record a drift event."""
        self._metrics.drift_detected = True
        detail = {
            "kind": kind,
            "expected": expected[:12],
            "actual": actual[:12],
        }
        self._metrics.drift_details.append(detail)
        logger.warning(
            "DRIFT_DETECTED: %s hash changed (expected=%s, actual=%s)",
            kind,
            expected[:12],
            actual[:12],
        )

    # -- finalization --------------------------------------------------------

    def finalize(self) -> RunMetrics:
        """Finalize metrics and log summary."""
        self._metrics.completed_at = time.monotonic()

        logger.info(
            "Run %s completed: duration=%.0fms turns=%d tools=%d "
            "memory=%d/%d/%d heartbeats=%d drift=%s",
            self._metrics.run_id,
            self._metrics.duration_ms,
            self._metrics.turn_count,
            self._metrics.tool_count,
            self._metrics.memory_committed,
            self._metrics.memory_deduplicated,
            self._metrics.memory_gated,
            self._metrics.heartbeat_count,
            self._metrics.drift_detected,
        )

        return self._metrics
