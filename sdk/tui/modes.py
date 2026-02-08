"""
sdk.tui.modes -- TUI mode system.

Defines the TUIMode enum and ModeManager state machine that governs
mode transitions and mode-specific behavior (system prompts, allowed
operations, UI layout changes).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Mode enum
# ---------------------------------------------------------------------------

class TUIMode(enum.Enum):
    """Available TUI interaction modes."""

    ASK = "ask"
    PLAN = "plan"
    CODE = "code"
    DIFF = "diff"


# ---------------------------------------------------------------------------
# Plan step
# ---------------------------------------------------------------------------

@dataclass
class PlanStep:
    """A single step in a structured plan."""

    number: int
    description: str
    status: str = "pending"  # "pending" | "approved" | "rejected" | "edited"
    original: str = ""       # Original text before edits

    def approve(self) -> None:
        self.status = "approved"

    def reject(self) -> None:
        self.status = "rejected"

    def edit(self, new_description: str) -> None:
        if not self.original:
            self.original = self.description
        self.description = new_description
        self.status = "edited"


@dataclass
class Plan:
    """A structured plan with numbered steps."""

    title: str
    steps: list[PlanStep] = field(default_factory=list)
    raw_text: str = ""

    @property
    def approved_count(self) -> int:
        return sum(
            1 for s in self.steps
            if s.status in ("approved", "edited")
        )

    @property
    def rejected_count(self) -> int:
        return sum(1 for s in self.steps if s.status == "rejected")

    @property
    def pending_count(self) -> int:
        return sum(1 for s in self.steps if s.status == "pending")

    @property
    def all_decided(self) -> bool:
        return all(s.status != "pending" for s in self.steps)

    @classmethod
    def parse(cls, text: str) -> Plan:
        """Parse a numbered plan from assistant response text.

        Recognizes patterns like:
            1. Step description
            2. Another step
        or:
            1) Step description
            2) Another step
        """
        import re

        lines = text.strip().split("\n")
        title = ""
        steps: list[PlanStep] = []

        # Try to extract a title from the first non-numbered line
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if re.match(r"^\d+[\.\)]\s", stripped):
                break
            if not title:
                # Remove markdown headers
                title = re.sub(r"^#+\s*", "", stripped)

        # Extract numbered steps
        step_pattern = re.compile(r"^\s*(\d+)[\.\)]\s+(.+)")
        current_step_num = 0
        current_step_text = ""

        for line in lines:
            match = step_pattern.match(line)
            if match:
                # Save previous step
                if current_step_num > 0:
                    steps.append(PlanStep(
                        number=current_step_num,
                        description=current_step_text.strip(),
                    ))
                current_step_num = int(match.group(1))
                current_step_text = match.group(2)
            elif current_step_num > 0 and line.strip():
                # Continuation of current step
                current_step_text += " " + line.strip()

        # Don't forget the last step
        if current_step_num > 0:
            steps.append(PlanStep(
                number=current_step_num,
                description=current_step_text.strip(),
            ))

        if not title:
            title = "Implementation Plan"

        return cls(title=title, steps=steps, raw_text=text)


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

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
        "You are reviewing code changes. Analyze the diffs provided and "
        "give feedback on correctness, style, and potential issues."
    ),
}


# ---------------------------------------------------------------------------
# FileChange (used by Code/Diff modes -- re-exported from diff_engine)
# ---------------------------------------------------------------------------

@dataclass
class FileChange:
    """A tracked file change from Code mode."""

    path: Path
    original: str
    modified: str
    status: str = "pending"  # "pending" | "accepted" | "rejected"


# ---------------------------------------------------------------------------
# ModeManager
# ---------------------------------------------------------------------------

class ModeManager:
    """State machine for TUI mode transitions.

    Tracks the current mode, pending file changes from Code mode,
    the active plan from Plan mode, and provides mode-specific
    system prompts.
    """

    def __init__(self, initial: TUIMode = TUIMode.ASK) -> None:
        self._current = initial
        self._pending_changes: list[FileChange] = []
        self._active_plan: Plan | None = None
        self._listeners: list[Any] = []

    # -- Properties ---------------------------------------------------------

    @property
    def current(self) -> TUIMode:
        return self._current

    @property
    def pending_changes(self) -> list[FileChange]:
        return self._pending_changes

    @property
    def active_plan(self) -> Plan | None:
        return self._active_plan

    @active_plan.setter
    def active_plan(self, plan: Plan | None) -> None:
        self._active_plan = plan

    # -- Transitions --------------------------------------------------------

    def switch(self, mode: TUIMode) -> None:
        """Switch to a new mode.

        Validates the transition and notifies listeners.
        """
        old = self._current
        self._current = mode
        for listener in self._listeners:
            listener(old, mode)

    def on_switch(self, callback: Any) -> None:
        """Register a mode-switch listener: callback(old_mode, new_mode)."""
        self._listeners.append(callback)

    # -- System prompt ------------------------------------------------------

    def get_system_prompt(self) -> str:
        """Return the mode-specific system prompt prefix."""
        base = _MODE_SYSTEM_PROMPTS.get(self._current, "")

        # In Code mode, include approved plan context if available
        if self._current == TUIMode.CODE and self._active_plan:
            approved = [
                s for s in self._active_plan.steps
                if s.status in ("approved", "edited")
            ]
            if approved:
                plan_ctx = "\n".join(
                    f"{s.number}. {s.description}" for s in approved
                )
                base += (
                    f"\n\nApproved plan to execute:\n{plan_ctx}\n\n"
                    "Implement these steps in order."
                )

        # In Diff mode, include pending changes context
        if self._current == TUIMode.DIFF and self._pending_changes:
            files = [str(c.path) for c in self._pending_changes]
            base += (
                f"\n\nFiles with pending changes: {', '.join(files)}"
            )

        return base

    # -- File changes -------------------------------------------------------

    def add_change(self, change: FileChange) -> None:
        """Track a file change from Code mode."""
        # Replace existing change for same path
        self._pending_changes = [
            c for c in self._pending_changes if c.path != change.path
        ]
        self._pending_changes.append(change)

    def clear_changes(self) -> None:
        """Clear all pending changes."""
        self._pending_changes.clear()

    def get_change(self, path: Path) -> FileChange | None:
        """Get a pending change by path."""
        for c in self._pending_changes:
            if c.path == path:
                return c
        return None
