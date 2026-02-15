"""
sdk.tui.widgets.plan_view -- Numbered plan with approve/reject UI.

Displays a structured plan parsed from assistant responses, with
per-step approve/reject/edit controls and an overall summary bar.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Static

from sdk.tui.modes import Plan, PlanStep


# ---------------------------------------------------------------------------
# PlanStepWidget
# ---------------------------------------------------------------------------

class PlanStepWidget(Widget):
    """A single plan step with approve/reject controls."""

    DEFAULT_CSS = """
    PlanStepWidget {
        height: auto;
        padding: 0 0 0 2;
    }
    """

    class StepApproved(Message):
        """Emitted when a step is approved."""

        def __init__(self, step_number: int) -> None:
            super().__init__()
            self.step_number = step_number

    class StepRejected(Message):
        """Emitted when a step is rejected."""

        def __init__(self, step_number: int) -> None:
            super().__init__()
            self.step_number = step_number

    def __init__(
        self,
        step: PlanStep,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        base_classes = f"plan-step {step.status}"
        if classes:
            base_classes += f" {classes}"
        super().__init__(name=name, id=id, classes=base_classes)
        self._step = step

    def compose(self) -> ComposeResult:
        with Horizontal():
            # Step number
            yield Static(
                f"{self._step.number}.",
                classes="step-number",
            )
            # Step text
            yield Static(
                f" {self._step.description}",
                classes="step-text",
                id=f"step-text-{self._step.number}",
            )
            # Status indicator
            status_text = self._status_text()
            yield Static(
                status_text,
                classes="step-status",
                id=f"step-status-{self._step.number}",
            )

    def _status_text(self) -> str:
        """Get the status indicator text."""
        status_map = {
            "pending": " [y/n]",
            "approved": " [OK]",
            "rejected": " [NO]",
            "edited": " [EDIT]",
        }
        return status_map.get(self._step.status, "")

    async def on_key(self, event: any) -> None:
        """Handle y/n keys for approve/reject."""
        if event.key == "y" and self._step.status == "pending":
            self._step.approve()
            self._update_display()
            self.post_message(self.StepApproved(self._step.number))
            event.stop()
        elif event.key == "n" and self._step.status == "pending":
            self._step.reject()
            self._update_display()
            self.post_message(self.StepRejected(self._step.number))
            event.stop()

    def _update_display(self) -> None:
        """Update the visual state after approve/reject."""
        # Update CSS classes
        self.remove_class("pending")
        self.remove_class("approved")
        self.remove_class("rejected")
        self.remove_class("edited")
        self.add_class(self._step.status)

        # Update status text
        try:
            status = self.query_one(
                f"#step-status-{self._step.number}", Static
            )
            status.update(self._status_text())
        except Exception:
            pass

    @property
    def step(self) -> PlanStep:
        return self._step


# ---------------------------------------------------------------------------
# PlanView
# ---------------------------------------------------------------------------

class PlanView(Widget):
    """Displays a structured plan with per-step approval UI.

    Shows:
    - Plan title
    - Numbered steps with approve/reject controls
    - Summary bar: "X/Y steps approved"
    - Execute button when all steps are decided
    """

    DEFAULT_CSS = """
    PlanView {
        height: auto;
        padding: 1 2;
    }
    """

    class PlanApproved(Message):
        """Emitted when the plan is fully approved and ready to execute."""

        def __init__(self, plan: Plan) -> None:
            super().__init__()
            self.plan = plan

    class PlanRejected(Message):
        """Emitted when the plan is fully rejected."""

        def __init__(self, plan: Plan) -> None:
            super().__init__()
            self.plan = plan

    def __init__(
        self,
        plan: Plan | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._plan = plan
        self._step_widgets: list[PlanStepWidget] = []
        self._summary_widget: Static | None = None
        self._execute_btn: Button | None = None
        self._container: Vertical | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="plan-view"):
            if self._plan:
                yield Static(
                    self._plan.title,
                    classes="plan-title",
                )
                for step in self._plan.steps:
                    yield PlanStepWidget(
                        step,
                        id=f"plan-step-{step.number}",
                    )
                yield Static(
                    self._summary_text(),
                    classes="plan-summary",
                    id="plan-summary",
                )
                yield Button(
                    "Execute Plan",
                    id="execute-plan-btn",
                    variant="primary",
                    disabled=True,
                )
            else:
                yield Static(
                    "No plan loaded. Send a task in Plan mode to generate one.",
                    classes="plan-empty",
                )

    def on_mount(self) -> None:
        """Cache widget references."""
        try:
            self._container = self.query_one(Vertical)
            self._summary_widget = self.query_one("#plan-summary", Static)
            self._execute_btn = self.query_one("#execute-plan-btn", Button)
        except Exception:
            pass

        self._step_widgets = list(self.query(PlanStepWidget))

    def _summary_text(self) -> str:
        """Build the summary text."""
        if not self._plan:
            return ""
        total = len(self._plan.steps)
        approved = self._plan.approved_count
        rejected = self._plan.rejected_count
        pending = self._plan.pending_count
        return (
            f"{approved}/{total} approved, "
            f"{rejected} rejected, "
            f"{pending} pending"
        )

    # -- Event handlers -----------------------------------------------------

    def on_plan_step_widget_step_approved(
        self, event: PlanStepWidget.StepApproved
    ) -> None:
        """Handle step approval."""
        self._update_summary()

    def on_plan_step_widget_step_rejected(
        self, event: PlanStepWidget.StepRejected
    ) -> None:
        """Handle step rejection."""
        self._update_summary()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle Execute Plan button press."""
        if event.button.id == "execute-plan-btn" and self._plan:
            if self._plan.approved_count > 0:
                self.post_message(self.PlanApproved(self._plan))
            else:
                self.post_message(self.PlanRejected(self._plan))

    def _update_summary(self) -> None:
        """Update the summary bar and execute button state."""
        if self._summary_widget and self._plan:
            self._summary_widget.update(self._summary_text())

        if self._execute_btn and self._plan:
            self._execute_btn.disabled = not self._plan.all_decided

    # -- Public API ---------------------------------------------------------

    def set_plan(self, plan: Plan) -> None:
        """Replace the current plan and re-render.

        Args:
            plan: The new Plan to display.
        """
        self._plan = plan
        self._rebuild()

    def approve_all(self) -> None:
        """Approve all pending steps."""
        if not self._plan:
            return
        for step in self._plan.steps:
            if step.status == "pending":
                step.approve()
        self._rebuild()

    def reject_all(self) -> None:
        """Reject all pending steps."""
        if not self._plan:
            return
        for step in self._plan.steps:
            if step.status == "pending":
                step.reject()
        self._rebuild()

    def _rebuild(self) -> None:
        """Rebuild the plan view from scratch."""
        if self._container is None:
            return

        # Remove all children
        for child in list(self._container.children):
            child.remove()

        if not self._plan:
            self._container.mount(
                Static(
                    "No plan loaded.",
                    classes="plan-empty",
                )
            )
            return

        # Title
        self._container.mount(
            Static(self._plan.title, classes="plan-title")
        )

        # Steps
        self._step_widgets.clear()
        for step in self._plan.steps:
            w = PlanStepWidget(step, id=f"plan-step-{step.number}")
            self._step_widgets.append(w)
            self._container.mount(w)

        # Summary
        summary = Static(
            self._summary_text(),
            classes="plan-summary",
            id="plan-summary",
        )
        self._summary_widget = summary
        self._container.mount(summary)

        # Execute button
        btn = Button(
            "Execute Plan",
            id="execute-plan-btn",
            variant="primary",
            disabled=not self._plan.all_decided,
        )
        self._execute_btn = btn
        self._container.mount(btn)

    @property
    def plan(self) -> Plan | None:
        return self._plan

    @property
    def is_ready(self) -> bool:
        """Whether all steps have been decided."""
        return self._plan.all_decided if self._plan else False
