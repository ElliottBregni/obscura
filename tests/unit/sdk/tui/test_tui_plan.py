"""Tests for sdk.tui plan parsing and approval flow.

Covers plan step parsing from agent responses, per-step approve/reject,
plan summary (X/Y steps approved), 'Execute Plan' trigger when all decided,
and plan storage in session memory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Inline stubs — mirrors plan-related types from PLAN_TUI.md
# ---------------------------------------------------------------------------


class TUIMode(Enum):
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

    def approve(self) -> None:
        self.status = "approved"

    def reject(self) -> None:
        self.status = "rejected"

    def reset(self) -> None:
        self.status = "pending"


@dataclass
class Plan:
    """A structured plan with numbered steps."""

    steps: list[PlanStep] = field(default_factory=list)
    summary: str = ""

    @property
    def all_decided(self) -> bool:
        return all(s.status != "pending" for s in self.steps)

    @property
    def approved_count(self) -> int:
        return sum(1 for s in self.steps if s.status == "approved")

    @property
    def rejected_count(self) -> int:
        return sum(1 for s in self.steps if s.status == "rejected")

    @property
    def pending_count(self) -> int:
        return sum(1 for s in self.steps if s.status == "pending")

    @property
    def total_count(self) -> int:
        return len(self.steps)

    def summary_text(self) -> str:
        return f"{self.approved_count}/{self.total_count} steps approved"

    @property
    def ready_to_execute(self) -> bool:
        return self.all_decided and self.approved_count > 0


def parse_plan_from_response(text: str) -> Plan:
    """Parse a numbered plan from an agent response.

    Expects lines matching patterns like:
        1. Do something
        2. Do another thing
    or:
        1) Do something
        2) Do another thing
    """
    steps: list[PlanStep] = []
    pattern = re.compile(r"^\s*(\d+)[.)]\s+(.+)$", re.MULTILINE)
    for match in pattern.finditer(text):
        idx = int(match.group(1))
        desc = match.group(2).strip()
        steps.append(PlanStep(index=idx, description=desc))
    return Plan(steps=steps)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPlanStepCreation:
    """Verify PlanStep construction and defaults."""

    def test_create_step_with_defaults(self) -> None:
        """PlanStep defaults to 'pending' status."""
        step = PlanStep(index=1, description="Create the module")
        assert step.index == 1
        assert step.description == "Create the module"
        assert step.status == "pending"

    def test_create_step_with_explicit_status(self) -> None:
        """PlanStep accepts explicit status."""
        step = PlanStep(index=2, description="Write tests", status="approved")
        assert step.status == "approved"

    def test_approve_step(self) -> None:
        """approve() sets status to 'approved'."""
        step = PlanStep(index=1, description="S1")
        step.approve()
        assert step.status == "approved"

    def test_reject_step(self) -> None:
        """reject() sets status to 'rejected'."""
        step = PlanStep(index=1, description="S1")
        step.reject()
        assert step.status == "rejected"

    def test_reset_step(self) -> None:
        """reset() returns status to 'pending'."""
        step = PlanStep(index=1, description="S1", status="approved")
        step.reset()
        assert step.status == "pending"

    def test_approve_then_reject(self) -> None:
        """Step can be approved then rejected (last action wins)."""
        step = PlanStep(index=1, description="S1")
        step.approve()
        step.reject()
        assert step.status == "rejected"

    def test_reject_then_approve(self) -> None:
        """Step can be rejected then approved (last action wins)."""
        step = PlanStep(index=1, description="S1")
        step.reject()
        step.approve()
        assert step.status == "approved"


class TestPlanParsing:
    """Verify parse_plan_from_response extracts steps correctly."""

    def test_parse_dot_numbered_list(self) -> None:
        """Parses '1. Step' format."""
        response = (
            "Here is my plan:\n"
            "1. Create the database schema\n"
            "2. Write the migration script\n"
            "3. Add seed data\n"
        )
        plan = parse_plan_from_response(response)
        assert len(plan.steps) == 3
        assert plan.steps[0].description == "Create the database schema"
        assert plan.steps[1].description == "Write the migration script"
        assert plan.steps[2].description == "Add seed data"

    def test_parse_paren_numbered_list(self) -> None:
        """Parses '1) Step' format."""
        response = "Steps:\n1) Read the config file\n2) Validate inputs\n"
        plan = parse_plan_from_response(response)
        assert len(plan.steps) == 2
        assert plan.steps[0].index == 1
        assert plan.steps[1].index == 2

    def test_parse_with_surrounding_prose(self) -> None:
        """Steps are extracted even when surrounded by other text."""
        response = (
            "I'll approach this in phases.\n\n"
            "1. Analyze the existing codebase\n"
            "2. Identify areas for improvement\n\n"
            "This should take about a day."
        )
        plan = parse_plan_from_response(response)
        assert len(plan.steps) == 2

    def test_parse_preserves_step_indices(self) -> None:
        """Parsed step indices match the source numbers."""
        response = "1. First\n2. Second\n3. Third\n"
        plan = parse_plan_from_response(response)
        indices = [s.index for s in plan.steps]
        assert indices == [1, 2, 3]

    def test_parse_empty_response(self) -> None:
        """Empty response produces empty plan."""
        plan = parse_plan_from_response("")
        assert len(plan.steps) == 0

    def test_parse_response_with_no_numbered_steps(self) -> None:
        """Response without numbered steps produces empty plan."""
        response = "I think we should refactor. Start with the auth module."
        plan = parse_plan_from_response(response)
        assert len(plan.steps) == 0

    def test_parse_indented_steps(self) -> None:
        """Indented numbered steps are still parsed."""
        response = "  1. First step\n  2. Second step\n"
        plan = parse_plan_from_response(response)
        assert len(plan.steps) == 2

    def test_parse_steps_default_to_pending(self) -> None:
        """All parsed steps start with 'pending' status."""
        response = "1. Step A\n2. Step B\n"
        plan = parse_plan_from_response(response)
        for step in plan.steps:
            assert step.status == "pending"

    def test_parse_single_step(self) -> None:
        """A single-step plan is parsed correctly."""
        response = "1. Just do it\n"
        plan = parse_plan_from_response(response)
        assert len(plan.steps) == 1
        assert plan.steps[0].description == "Just do it"

    def test_parse_many_steps(self) -> None:
        """A plan with many steps is parsed fully."""
        lines = [f"{i}. Step number {i}" for i in range(1, 21)]
        response = "\n".join(lines)
        plan = parse_plan_from_response(response)
        assert len(plan.steps) == 20
        assert plan.steps[19].index == 20


class TestPlanApprovalFlow:
    """Verify per-step approve/reject and summary calculations."""

    @pytest.fixture()
    def three_step_plan(self) -> Plan:
        """A plan with three pending steps."""
        return Plan(
            steps=[
                PlanStep(index=1, description="Step 1"),
                PlanStep(index=2, description="Step 2"),
                PlanStep(index=3, description="Step 3"),
            ]
        )

    def test_initial_plan_all_pending(self, three_step_plan: Plan) -> None:
        """A fresh plan has all steps pending."""
        assert three_step_plan.pending_count == 3
        assert three_step_plan.approved_count == 0
        assert three_step_plan.rejected_count == 0

    def test_all_decided_false_initially(self, three_step_plan: Plan) -> None:
        """all_decided is False when all steps are pending."""
        assert three_step_plan.all_decided is False

    def test_approve_single_step(self, three_step_plan: Plan) -> None:
        """Approving one step updates counts."""
        three_step_plan.steps[0].approve()
        assert three_step_plan.approved_count == 1
        assert three_step_plan.pending_count == 2

    def test_reject_single_step(self, three_step_plan: Plan) -> None:
        """Rejecting one step updates counts."""
        three_step_plan.steps[1].reject()
        assert three_step_plan.rejected_count == 1
        assert three_step_plan.pending_count == 2

    def test_all_decided_partial(self, three_step_plan: Plan) -> None:
        """all_decided is False when only some steps are decided."""
        three_step_plan.steps[0].approve()
        three_step_plan.steps[1].reject()
        assert three_step_plan.all_decided is False

    def test_all_decided_all_approved(self, three_step_plan: Plan) -> None:
        """all_decided is True when all steps are approved."""
        for step in three_step_plan.steps:
            step.approve()
        assert three_step_plan.all_decided is True

    def test_all_decided_mix_approved_rejected(self, three_step_plan: Plan) -> None:
        """all_decided is True with a mix of approved and rejected."""
        three_step_plan.steps[0].approve()
        three_step_plan.steps[1].reject()
        three_step_plan.steps[2].approve()
        assert three_step_plan.all_decided is True

    def test_all_decided_all_rejected(self, three_step_plan: Plan) -> None:
        """all_decided is True when all steps are rejected."""
        for step in three_step_plan.steps:
            step.reject()
        assert three_step_plan.all_decided is True

    def test_summary_text_initial(self, three_step_plan: Plan) -> None:
        """Summary shows 0/3 initially."""
        assert three_step_plan.summary_text() == "0/3 steps approved"

    def test_summary_text_partial(self, three_step_plan: Plan) -> None:
        """Summary updates as steps are approved."""
        three_step_plan.steps[0].approve()
        three_step_plan.steps[2].approve()
        assert three_step_plan.summary_text() == "2/3 steps approved"

    def test_summary_text_all_approved(self, three_step_plan: Plan) -> None:
        """Summary shows full count when all approved."""
        for step in three_step_plan.steps:
            step.approve()
        assert three_step_plan.summary_text() == "3/3 steps approved"


class TestExecutePlanTrigger:
    """Verify the 'Execute Plan' trigger when all steps are decided."""

    def test_ready_to_execute_when_all_approved(self) -> None:
        """Plan is ready to execute when all steps are approved."""
        plan = Plan(
            steps=[
                PlanStep(index=1, description="S1", status="approved"),
                PlanStep(index=2, description="S2", status="approved"),
            ]
        )
        assert plan.ready_to_execute is True

    def test_ready_to_execute_with_mix(self) -> None:
        """Plan is ready when all decided and at least one approved."""
        plan = Plan(
            steps=[
                PlanStep(index=1, description="S1", status="approved"),
                PlanStep(index=2, description="S2", status="rejected"),
            ]
        )
        assert plan.ready_to_execute is True

    def test_not_ready_when_pending(self) -> None:
        """Plan is not ready when any step is still pending."""
        plan = Plan(
            steps=[
                PlanStep(index=1, description="S1", status="approved"),
                PlanStep(index=2, description="S2", status="pending"),
            ]
        )
        assert plan.ready_to_execute is False

    def test_not_ready_when_all_rejected(self) -> None:
        """Plan is not ready when all steps are rejected (nothing to execute)."""
        plan = Plan(
            steps=[
                PlanStep(index=1, description="S1", status="rejected"),
                PlanStep(index=2, description="S2", status="rejected"),
            ]
        )
        assert plan.ready_to_execute is False

    def test_empty_plan_not_ready(self) -> None:
        """Empty plan is not ready to execute."""
        plan = Plan(steps=[])
        assert plan.ready_to_execute is False


class TestPlanInSessionMemory:
    """Verify that plans can be stored and retrieved through session-like patterns."""

    def test_plan_to_dict_roundtrip(self) -> None:
        """Plan data can be serialized to dict and reconstructed."""
        original = Plan(
            steps=[
                PlanStep(index=1, description="Create schema", status="approved"),
                PlanStep(index=2, description="Write migrations", status="rejected"),
                PlanStep(index=3, description="Add tests", status="pending"),
            ]
        )

        # Simulate serialization
        data = {
            "steps": [
                {"index": s.index, "description": s.description, "status": s.status}
                for s in original.steps
            ],
            "summary": original.summary,
        }

        # Simulate deserialization
        restored = Plan(
            steps=[PlanStep(**s) for s in data["steps"]],
            summary=data["summary"],
        )

        assert len(restored.steps) == 3
        assert restored.steps[0].status == "approved"
        assert restored.steps[1].status == "rejected"
        assert restored.steps[2].status == "pending"

    def test_plan_stored_as_metadata(self) -> None:
        """A plan can be stored in turn metadata for session persistence."""
        plan = Plan(
            steps=[
                PlanStep(index=1, description="Do X"),
                PlanStep(index=2, description="Do Y"),
            ]
        )
        plan.steps[0].approve()

        metadata: dict[str, Any] = {
            "plan": {
                "steps": [
                    {"index": s.index, "description": s.description, "status": s.status}
                    for s in plan.steps
                ],
            },
        }

        # Verify the metadata structure
        assert len(metadata["plan"]["steps"]) == 2
        assert metadata["plan"]["steps"][0]["status"] == "approved"
        assert metadata["plan"]["steps"][1]["status"] == "pending"

    def test_modify_step_after_initial_decision(self) -> None:
        """User can change their mind and re-approve/reject a step."""
        plan = Plan(
            steps=[
                PlanStep(index=1, description="Step 1"),
            ]
        )
        plan.steps[0].approve()
        assert plan.steps[0].status == "approved"

        plan.steps[0].reject()
        assert plan.steps[0].status == "rejected"

        plan.steps[0].reset()
        assert plan.steps[0].status == "pending"
        assert plan.all_decided is False
