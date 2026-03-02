"""
obscura.core.supervisor.state_machine — Deterministic session state machine.

Enforces valid transitions, records every transition as an event,
and provides invariant checking at each state boundary.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from obscura.core.supervisor.errors import StateTransitionError
from obscura.core.supervisor.types import (
    SupervisorEvent,
    SupervisorEventKind,
    SupervisorState,
    VALID_SUPERVISOR_TRANSITIONS,
)

logger = logging.getLogger(__name__)


class SessionStateMachine:
    """Deterministic state machine for a single supervisor run.

    Thread-safe for reads. Mutations (``transition``) must be called
    from a single writer (the supervisor itself).

    Usage::

        sm = SessionStateMachine(session_id="sess-1", run_id="run-abc")
        sm.transition(SupervisorState.BUILDING_CONTEXT)
        assert sm.state == SupervisorState.BUILDING_CONTEXT
        sm.transition(SupervisorState.RUNNING_MODEL)
        # sm.transition(SupervisorState.IDLE)  → raises StateTransitionError
    """

    def __init__(
        self,
        session_id: str,
        run_id: str,
        *,
        initial_state: SupervisorState = SupervisorState.IDLE,
    ) -> None:
        self._session_id = session_id
        self._run_id = run_id
        self._state = initial_state
        self._history: list[SupervisorEvent] = []
        self._transition_count = 0

    # -- properties ----------------------------------------------------------

    @property
    def state(self) -> SupervisorState:
        """Current state (read-only)."""
        return self._state

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def transition_count(self) -> int:
        return self._transition_count

    @property
    def history(self) -> list[SupervisorEvent]:
        """Ordered list of transition events."""
        return list(self._history)

    # -- transitions ---------------------------------------------------------

    def can_transition(self, target: SupervisorState) -> bool:
        """Check if a transition to ``target`` is valid from current state."""
        return target in VALID_SUPERVISOR_TRANSITIONS.get(self._state, frozenset())

    def transition(
        self,
        target: SupervisorState,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> SupervisorEvent:
        """Transition to a new state.

        Raises:
            StateTransitionError: If the transition is not valid.

        Returns:
            The state transition event (for logging/persistence).
        """
        if not self.can_transition(target):
            raise StateTransitionError(self._state.value, target.value)

        from_state = self._state
        self._state = target
        self._transition_count += 1

        event = SupervisorEvent(
            kind=SupervisorEventKind.STATE_TRANSITION,
            run_id=self._run_id,
            session_id=self._session_id,
            payload={
                "from_state": from_state.value,
                "to_state": target.value,
                "transition_number": self._transition_count,
                **(metadata or {}),
            },
        )
        self._history.append(event)

        logger.debug(
            "State transition: %s -> %s (run=%s, #%d)",
            from_state.value,
            target.value,
            self._run_id,
            self._transition_count,
        )
        return event

    def fail(
        self,
        error: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> SupervisorEvent:
        """Transition to FAILED from any non-terminal state.

        FAILED is reachable from every state except IDLE.
        """
        if self._state == SupervisorState.IDLE:
            raise StateTransitionError(self._state.value, SupervisorState.FAILED.value)

        from_state = self._state
        self._state = SupervisorState.FAILED
        self._transition_count += 1

        event = SupervisorEvent(
            kind=SupervisorEventKind.STATE_TRANSITION,
            run_id=self._run_id,
            session_id=self._session_id,
            payload={
                "from_state": from_state.value,
                "to_state": SupervisorState.FAILED.value,
                "transition_number": self._transition_count,
                "error": error,
                **(metadata or {}),
            },
        )
        self._history.append(event)

        logger.warning(
            "State failed: %s -> FAILED (run=%s, error=%s)",
            from_state.value,
            self._run_id,
            error,
        )
        return event

    def reset(self) -> SupervisorEvent:
        """Reset to IDLE from FAILED or FINALIZING.

        Used for recovery or normal run completion.
        """
        if self._state not in (SupervisorState.FAILED, SupervisorState.FINALIZING):
            raise StateTransitionError(self._state.value, SupervisorState.IDLE.value)

        from_state = self._state
        self._state = SupervisorState.IDLE
        self._transition_count += 1

        event = SupervisorEvent(
            kind=SupervisorEventKind.STATE_TRANSITION,
            run_id=self._run_id,
            session_id=self._session_id,
            payload={
                "from_state": from_state.value,
                "to_state": SupervisorState.IDLE.value,
                "transition_number": self._transition_count,
            },
        )
        self._history.append(event)
        return event

    # -- invariant checks ----------------------------------------------------

    def assert_state(self, expected: SupervisorState) -> None:
        """Assert the machine is in an expected state.

        Raises:
            StateTransitionError: If current state doesn't match.
        """
        if self._state != expected:
            raise StateTransitionError(
                f"Expected {expected.value}, currently in {self._state.value}",
                "assertion",
            )

    def assert_not_idle(self) -> None:
        """Assert we're in an active run (not IDLE)."""
        if self._state == SupervisorState.IDLE:
            raise StateTransitionError("idle", "any active state")
