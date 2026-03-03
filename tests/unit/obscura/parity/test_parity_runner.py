from __future__ import annotations

from obscura.core.types import Backend
from obscura.parity.models import ScenarioResult, ScenarioSpec
from obscura.parity.runner import run_scenarios
from obscura.parity.scenarios import SCENARIOS


class FakeExecutor:
    def execute(self, spec: ScenarioSpec) -> ScenarioResult:
        if spec.id == "copilot.stream.lifecycle":
            return ScenarioResult(
                scenario_id=spec.id,
                backend=Backend.COPILOT,
                passed=True,
                observed_events=(
                    "assistant.message_delta",
                    "tool.execution_start",
                    "session.idle",
                ),
            )
        return ScenarioResult(
            scenario_id=spec.id,
            backend=spec.backend,
            passed=True,
            observed_events=("response.completed",),
        )


def test_run_scenarios_matches_expectations() -> None:
    checks = run_scenarios(SCENARIOS, FakeExecutor())
    assert checks
    assert all(c.matched for c in checks)
