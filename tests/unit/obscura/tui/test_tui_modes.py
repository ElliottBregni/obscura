"""Tests for sdk.tui.modes — TUIMode enum and ModeManager state machine.

Covers mode enum values, state transitions, mode-specific system prompts,
invalid transitions, pending_changes tracking, and active_plan lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Inline stubs — mirrors sdk/tui/modes.py interfaces from PLAN_TUI.md
# These will be replaced by real imports once the module is implemented.
# ---------------------------------------------------------------------------


class TUIMode(Enum):
    """Modes available in the TUI."""

    ASK = "ask"
    PLAN = "plan"
    CODE = "code"
    DIFF = "diff"


@dataclass
class PlanStep:
    """A single step in a plan."""

    index: int
    description: str
    status: str = "pending"  # "pending" | "approved" | "rejected"


@dataclass
class Plan:
    """A structured plan with numbered steps."""

    steps: list[PlanStep] = field(default_factory=lambda: list[PlanStep]())
    summary: str = ""

    @property
    def all_decided(self) -> bool:
        return all(s.status != "pending" for s in self.steps)

    @property
    def approved_count(self) -> int:
        return sum(1 for s in self.steps if s.status == "approved")


@dataclass
class FileChange:
    """A file modification tracked in Code mode."""

    path: Path
    original: str
    modified: str
    status: str = "pending"  # "pending" | "accepted" | "rejected"


_MODE_SYSTEM_PROMPTS: dict[TUIMode, str] = {
    TUIMode.ASK: "",
    TUIMode.PLAN: (
        "You are in planning mode. Respond with structured, numbered "
        "implementation plans. Each step should be actionable and specific. "
        "Do not write code yet."
    ),
    TUIMode.CODE: (
        "You are in code mode. Use tools to read and write files. "
        "Show your changes clearly. Explain each change briefly."
    ),
    TUIMode.DIFF: (
        "You are reviewing code changes. Analyze the diffs provided "
        "and give feedback on correctness, style, and potential issues."
    ),
}


class ModeManager:
    """State machine for TUI mode transitions."""

    def __init__(self, initial: TUIMode = TUIMode.ASK) -> None:
        self.current = initial
        self.pending_changes: list[FileChange] = []
        self.active_plan: Plan | None = None

    def switch(self, mode: TUIMode) -> None:
        if not isinstance(mode, TUIMode):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError(f"Invalid mode: {mode}")
        self.current = mode

    def get_system_prompt(self) -> str:
        return _MODE_SYSTEM_PROMPTS.get(self.current, "")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTUIMode:
    """Verify TUIMode enum values and membership."""

    def test_mode_values(self) -> None:
        """All four modes have the expected string values."""
        assert TUIMode.ASK.value == "ask"
        assert TUIMode.PLAN.value == "plan"
        assert TUIMode.CODE.value == "code"
        assert TUIMode.DIFF.value == "diff"

    def test_mode_count(self) -> None:
        """Exactly four modes exist."""
        assert len(TUIMode) == 4

    def test_mode_from_string(self) -> None:
        """Modes can be constructed from their string values."""
        assert TUIMode("ask") is TUIMode.ASK
        assert TUIMode("plan") is TUIMode.PLAN
        assert TUIMode("code") is TUIMode.CODE
        assert TUIMode("diff") is TUIMode.DIFF

    def test_mode_from_invalid_string_raises(self) -> None:
        """Invalid string raises ValueError."""
        with pytest.raises(ValueError):
            TUIMode("invalid")

    def test_modes_are_distinct(self) -> None:
        """Each mode is a distinct enum member."""
        modes = list(TUIMode)
        assert len(modes) == len(set(modes))

    def test_mode_identity(self) -> None:
        """Enum identity comparison works correctly."""
        assert TUIMode.ASK is TUIMode.ASK
        assert TUIMode.ASK is not TUIMode.PLAN


class TestModeManagerTransitions:
    """Verify ModeManager state transitions."""

    def test_default_mode_is_ask(self) -> None:
        """ModeManager starts in ASK mode by default."""
        mgr = ModeManager()
        assert mgr.current is TUIMode.ASK

    def test_custom_initial_mode(self) -> None:
        """ModeManager can start in any specified mode."""
        for mode in TUIMode:
            mgr = ModeManager(initial=mode)
            assert mgr.current is mode

    def test_switch_ask_to_plan(self) -> None:
        """Switching from ASK to PLAN updates current mode."""
        mgr = ModeManager()
        mgr.switch(TUIMode.PLAN)
        assert mgr.current is TUIMode.PLAN

    def test_switch_plan_to_code(self) -> None:
        """Switching from PLAN to CODE updates current mode."""
        mgr = ModeManager(initial=TUIMode.PLAN)
        mgr.switch(TUIMode.CODE)
        assert mgr.current is TUIMode.CODE

    def test_switch_code_to_diff(self) -> None:
        """Switching from CODE to DIFF updates current mode."""
        mgr = ModeManager(initial=TUIMode.CODE)
        mgr.switch(TUIMode.DIFF)
        assert mgr.current is TUIMode.DIFF

    def test_switch_to_same_mode(self) -> None:
        """Switching to the current mode is a no-op (stays the same)."""
        mgr = ModeManager()
        mgr.switch(TUIMode.ASK)
        assert mgr.current is TUIMode.ASK

    def test_full_cycle_through_all_modes(self) -> None:
        """Cycling through all modes in sequence works correctly."""
        mgr = ModeManager()
        for mode in [TUIMode.PLAN, TUIMode.CODE, TUIMode.DIFF, TUIMode.ASK]:
            mgr.switch(mode)
            assert mgr.current is mode

    def test_switch_invalid_type_raises(self) -> None:
        """Passing a non-TUIMode value raises ValueError."""
        mgr = ModeManager()
        with pytest.raises((ValueError, TypeError)):
            mgr.switch("ask")  # type: ignore[arg-type]

    def test_switch_none_raises(self) -> None:
        """Passing None raises ValueError."""
        mgr = ModeManager()
        with pytest.raises((ValueError, TypeError)):
            mgr.switch(None)  # type: ignore[arg-type]

    def test_rapid_mode_switching(self) -> None:
        """Rapid sequential mode switches settle on the last mode."""
        mgr = ModeManager()
        for _ in range(100):
            mgr.switch(TUIMode.PLAN)
            mgr.switch(TUIMode.CODE)
        assert mgr.current is TUIMode.CODE


class TestModeSystemPrompts:
    """Verify mode-specific system prompts."""

    def test_ask_mode_prompt_is_empty(self) -> None:
        """ASK mode uses the default (empty) system prompt."""
        mgr = ModeManager()
        assert mgr.get_system_prompt() == ""

    def test_plan_mode_prompt(self) -> None:
        """PLAN mode includes 'planning mode' instructions."""
        mgr = ModeManager(initial=TUIMode.PLAN)
        prompt = mgr.get_system_prompt()
        assert "planning mode" in prompt
        assert "Do not write code yet" in prompt

    def test_code_mode_prompt(self) -> None:
        """CODE mode includes 'code mode' instructions."""
        mgr = ModeManager(initial=TUIMode.CODE)
        prompt = mgr.get_system_prompt()
        assert "code mode" in prompt
        assert "read and write files" in prompt

    def test_diff_mode_prompt(self) -> None:
        """DIFF mode includes 'reviewing code changes' instructions."""
        mgr = ModeManager(initial=TUIMode.DIFF)
        prompt = mgr.get_system_prompt()
        assert "reviewing code changes" in prompt

    def test_prompt_updates_on_switch(self) -> None:
        """System prompt updates when mode is switched."""
        mgr = ModeManager()
        assert mgr.get_system_prompt() == ""
        mgr.switch(TUIMode.PLAN)
        assert "planning mode" in mgr.get_system_prompt()
        mgr.switch(TUIMode.CODE)
        assert "code mode" in mgr.get_system_prompt()

    def test_all_modes_have_prompts(self) -> None:
        """Every TUIMode has an entry in the prompt map."""
        for mode in TUIMode:
            mgr = ModeManager(initial=mode)
            prompt = mgr.get_system_prompt()
            assert isinstance(prompt, str)


class TestPendingChanges:
    """Verify pending_changes tracking across mode transitions."""

    def test_empty_pending_changes_on_init(self) -> None:
        """ModeManager starts with no pending changes."""
        mgr = ModeManager()
        assert mgr.pending_changes == []

    def test_add_pending_change(self) -> None:
        """File changes can be added to pending_changes."""
        mgr = ModeManager(initial=TUIMode.CODE)
        change = FileChange(
            path=Path("foo.py"),
            original="old",
            modified="new",
        )
        mgr.pending_changes.append(change)
        assert len(mgr.pending_changes) == 1
        assert mgr.pending_changes[0].path == Path("foo.py")

    def test_pending_changes_persist_across_mode_switch(self) -> None:
        """pending_changes survive switching from CODE to DIFF mode."""
        mgr = ModeManager(initial=TUIMode.CODE)
        mgr.pending_changes.append(
            FileChange(path=Path("a.py"), original="a", modified="b"),
        )
        mgr.switch(TUIMode.DIFF)
        assert len(mgr.pending_changes) == 1
        assert mgr.pending_changes[0].path == Path("a.py")

    def test_pending_changes_persist_across_all_mode_switches(self) -> None:
        """pending_changes survive cycling through all modes."""
        mgr = ModeManager(initial=TUIMode.CODE)
        mgr.pending_changes.append(
            FileChange(path=Path("keep.py"), original="x", modified="y"),
        )
        for mode in TUIMode:
            mgr.switch(mode)
        assert len(mgr.pending_changes) == 1

    def test_multiple_pending_changes(self) -> None:
        """Multiple file changes accumulate correctly."""
        mgr = ModeManager(initial=TUIMode.CODE)
        for i in range(5):
            mgr.pending_changes.append(
                FileChange(path=Path(f"file{i}.py"), original="", modified=f"v{i}"),
            )
        assert len(mgr.pending_changes) == 5

    def test_pending_change_status_tracking(self) -> None:
        """Individual change status can be updated independently."""
        mgr = ModeManager(initial=TUIMode.CODE)
        c1 = FileChange(path=Path("a.py"), original="", modified="a")
        c2 = FileChange(path=Path("b.py"), original="", modified="b")
        mgr.pending_changes.extend([c1, c2])

        mgr.pending_changes[0].status = "accepted"
        mgr.pending_changes[1].status = "rejected"

        assert mgr.pending_changes[0].status == "accepted"
        assert mgr.pending_changes[1].status == "rejected"


class TestActivePlan:
    """Verify active_plan lifecycle in ModeManager."""

    def test_no_active_plan_on_init(self) -> None:
        """ModeManager starts with no active plan."""
        mgr = ModeManager()
        assert mgr.active_plan is None

    def test_set_active_plan(self) -> None:
        """An active plan can be set on the ModeManager."""
        mgr = ModeManager(initial=TUIMode.PLAN)
        plan = Plan(
            steps=[PlanStep(index=1, description="Step 1")],
            summary="Test plan",
        )
        mgr.active_plan = plan
        assert mgr.active_plan is plan
        assert len(mgr.active_plan.steps) == 1

    def test_active_plan_persists_across_mode_switch(self) -> None:
        """active_plan survives mode transitions."""
        mgr = ModeManager(initial=TUIMode.PLAN)
        plan = Plan(steps=[PlanStep(index=1, description="Do something")])
        mgr.active_plan = plan

        mgr.switch(TUIMode.CODE)
        assert mgr.active_plan is plan

    def test_clear_active_plan(self) -> None:
        """active_plan can be explicitly cleared."""
        mgr = ModeManager(initial=TUIMode.PLAN)
        mgr.active_plan = Plan(steps=[PlanStep(index=1, description="S1")])
        mgr.active_plan = None
        assert mgr.active_plan is None

    def test_plan_all_decided_false_when_pending(self) -> None:
        """all_decided is False when any step is still pending."""
        plan = Plan(
            steps=[
                PlanStep(index=1, description="S1", status="approved"),
                PlanStep(index=2, description="S2", status="pending"),
            ]
        )
        assert plan.all_decided is False

    def test_plan_all_decided_true_when_all_approved(self) -> None:
        """all_decided is True when all steps are approved."""
        plan = Plan(
            steps=[
                PlanStep(index=1, description="S1", status="approved"),
                PlanStep(index=2, description="S2", status="approved"),
            ]
        )
        assert plan.all_decided is True

    def test_plan_all_decided_true_with_mix_approved_rejected(self) -> None:
        """all_decided is True with mix of approved and rejected."""
        plan = Plan(
            steps=[
                PlanStep(index=1, description="S1", status="approved"),
                PlanStep(index=2, description="S2", status="rejected"),
            ]
        )
        assert plan.all_decided is True

    def test_plan_approved_count(self) -> None:
        """approved_count returns correct count of approved steps."""
        plan = Plan(
            steps=[
                PlanStep(index=1, description="S1", status="approved"),
                PlanStep(index=2, description="S2", status="rejected"),
                PlanStep(index=3, description="S3", status="approved"),
            ]
        )
        assert plan.approved_count == 2

    def test_empty_plan_all_decided(self) -> None:
        """An empty plan has all_decided True (vacuous truth)."""
        plan = Plan(steps=[])
        assert plan.all_decided is True
        assert plan.approved_count == 0

    def test_replace_active_plan(self) -> None:
        """Setting a new plan replaces the old one."""
        mgr = ModeManager(initial=TUIMode.PLAN)
        plan1 = Plan(steps=[PlanStep(index=1, description="Old")])
        plan2 = Plan(steps=[PlanStep(index=1, description="New")])
        mgr.active_plan = plan1
        mgr.active_plan = plan2
        assert mgr.active_plan.steps[0].description == "New"
