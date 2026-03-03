"""Tests for the supervisor state machine."""

from __future__ import annotations

import pytest

from obscura.core.supervisor.errors import StateTransitionError
from obscura.core.supervisor.state_machine import SessionStateMachine
from obscura.core.supervisor.types import (
    SupervisorEventKind,
    SupervisorState,
    VALID_SUPERVISOR_TRANSITIONS,
)


class TestSessionStateMachine:
    """State machine enforces valid transitions and records history."""

    def test_initial_state_is_idle(self) -> None:
        sm = SessionStateMachine(session_id="s1", run_id="r1")
        assert sm.state == SupervisorState.IDLE
        assert sm.transition_count == 0

    def test_valid_transition_idle_to_building(self) -> None:
        sm = SessionStateMachine(session_id="s1", run_id="r1")
        event = sm.transition(SupervisorState.BUILDING_CONTEXT)
        assert sm.state == SupervisorState.BUILDING_CONTEXT
        assert sm.transition_count == 1
        assert event.kind == SupervisorEventKind.STATE_TRANSITION
        assert event.payload["from_state"] == "idle"
        assert event.payload["to_state"] == "building_context"

    def test_invalid_transition_raises(self) -> None:
        sm = SessionStateMachine(session_id="s1", run_id="r1")
        with pytest.raises(StateTransitionError):
            sm.transition(SupervisorState.RUNNING_TOOLS)

    def test_full_happy_path(self) -> None:
        sm = SessionStateMachine(session_id="s1", run_id="r1")
        sm.transition(SupervisorState.BUILDING_CONTEXT)
        sm.transition(SupervisorState.RUNNING_MODEL)
        sm.transition(SupervisorState.RUNNING_TOOLS)
        sm.transition(SupervisorState.RUNNING_MODEL)
        sm.transition(SupervisorState.COMMITTING_MEMORY)
        sm.transition(SupervisorState.FINALIZING)
        sm.reset()
        assert sm.state == SupervisorState.IDLE
        assert sm.transition_count == 7

    def test_fail_from_any_active_state(self) -> None:
        for start_state in [
            SupervisorState.BUILDING_CONTEXT,
            SupervisorState.RUNNING_MODEL,
            SupervisorState.RUNNING_TOOLS,
            SupervisorState.COMMITTING_MEMORY,
            SupervisorState.FINALIZING,
        ]:
            sm = SessionStateMachine(
                session_id="s1",
                run_id="r1",
                initial_state=start_state,
            )
            event = sm.fail("test error")
            assert sm.state == SupervisorState.FAILED
            assert "error" in event.payload

    def test_fail_from_idle_raises(self) -> None:
        sm = SessionStateMachine(session_id="s1", run_id="r1")
        with pytest.raises(StateTransitionError):
            sm.fail("should not work")

    def test_reset_from_failed(self) -> None:
        sm = SessionStateMachine(
            session_id="s1",
            run_id="r1",
            initial_state=SupervisorState.BUILDING_CONTEXT,
        )
        sm.fail("error")
        sm.reset()
        assert sm.state == SupervisorState.IDLE

    def test_reset_from_non_terminal_raises(self) -> None:
        sm = SessionStateMachine(
            session_id="s1",
            run_id="r1",
            initial_state=SupervisorState.RUNNING_MODEL,
        )
        with pytest.raises(StateTransitionError):
            sm.reset()

    def test_can_transition(self) -> None:
        sm = SessionStateMachine(session_id="s1", run_id="r1")
        assert sm.can_transition(SupervisorState.BUILDING_CONTEXT) is True
        assert sm.can_transition(SupervisorState.RUNNING_MODEL) is False

    def test_history_recorded(self) -> None:
        sm = SessionStateMachine(session_id="s1", run_id="r1")
        sm.transition(SupervisorState.BUILDING_CONTEXT)
        sm.transition(SupervisorState.RUNNING_MODEL)
        assert len(sm.history) == 2
        assert all(e.kind == SupervisorEventKind.STATE_TRANSITION for e in sm.history)

    def test_assert_state(self) -> None:
        sm = SessionStateMachine(session_id="s1", run_id="r1")
        sm.assert_state(SupervisorState.IDLE)  # should not raise
        with pytest.raises(StateTransitionError):
            sm.assert_state(SupervisorState.RUNNING_MODEL)

    def test_model_to_tools_loop(self) -> None:
        """Model ↔ Tools cycling is valid."""
        sm = SessionStateMachine(
            session_id="s1",
            run_id="r1",
            initial_state=SupervisorState.RUNNING_MODEL,
        )
        for _ in range(5):
            sm.transition(SupervisorState.RUNNING_TOOLS)
            sm.transition(SupervisorState.RUNNING_MODEL)
        assert sm.transition_count == 10

    def test_all_valid_transitions_covered(self) -> None:
        """Every state has defined transitions."""
        for state in SupervisorState:
            assert state in VALID_SUPERVISOR_TRANSITIONS
