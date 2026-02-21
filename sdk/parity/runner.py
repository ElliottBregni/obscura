"""Scenario runner for backend semantic parity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sdk.parity.models import ScenarioExpectation, ScenarioResult, ScenarioSpec


class ScenarioExecutor(Protocol):
    """Executes one scenario and returns observed behavior."""

    def execute(self, spec: ScenarioSpec) -> ScenarioResult: ...


@dataclass(frozen=True)
class ScenarioCheck:
    """Scenario result with expectation validation."""

    result: ScenarioResult
    expected: ScenarioExpectation

    @property
    def matched(self) -> bool:
        if self.result.passed != self.expected.should_pass:
            return False
        if not self.expected.expected_events:
            return True
        observed = set(self.result.observed_events)
        required = set(self.expected.expected_events)
        return required.issubset(observed)


def run_scenarios(
    scenarios: tuple[tuple[ScenarioSpec, ScenarioExpectation], ...],
    executor: ScenarioExecutor,
) -> tuple[ScenarioCheck, ...]:
    """Run scenario specs and validate against expectations."""
    checks: list[ScenarioCheck] = []
    for spec, expected in scenarios:
        result = executor.execute(spec)
        checks.append(ScenarioCheck(result=result, expected=expected))
    return tuple(checks)
