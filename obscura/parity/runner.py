"""Scenario runner for backend semantic parity.

Includes the original protocol-level runner plus the new
:class:`AgentLoopScenarioExecutor` that drives an AgentLoop through
step sequences with optional tool record/replay.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from obscura.core.types import AgentEvent, AgentEventKind, BackendProtocol
from obscura.parity.models import ScenarioExpectation, ScenarioResult, ScenarioSpec

if TYPE_CHECKING:
    from obscura.core.hooks import HookRegistry
    from obscura.core.tools import ToolRegistry

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# AgentLoop-based scenario executor
# ---------------------------------------------------------------------------


class AgentLoopScenarioExecutor:
    """Drives an :class:`AgentLoop` through a scenario with middleware.

    Responsibilities:
    1. Installs :class:`ToolRecordReplayMiddleware` based on ``tool_mode``.
    2. Runs the scenario's initial prompt through the agent loop.
    3. Collects all emitted events.
    4. Flushes recorded fixtures (if recording).
    5. Returns a :class:`ScenarioResult`.

    Usage::

        executor = AgentLoopScenarioExecutor(
            backend=my_backend,
            tool_registry=my_registry,
        )
        result = executor.execute(spec)
    """

    def __init__(
        self,
        backend: BackendProtocol,
        tool_registry: ToolRegistry,
        *,
        hooks: HookRegistry | None = None,
        max_turns: int = 10,
    ) -> None:
        self._backend = backend
        self._tool_registry = tool_registry
        self._hooks = hooks
        self._max_turns = max_turns

    def execute(self, spec: ScenarioSpec) -> ScenarioResult:
        """Run a scenario synchronously (wraps the async implementation)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # Already in an async context — create a new task
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, self._execute_async(spec))
                return future.result()
        return asyncio.run(self._execute_async(spec))

    async def execute_async(self, spec: ScenarioSpec) -> ScenarioResult:
        """Run a scenario asynchronously."""
        return await self._execute_async(spec)

    async def _execute_async(self, spec: ScenarioSpec) -> ScenarioResult:
        from obscura.core.agent_loop import AgentLoop
        from obscura.core.hooks import HookRegistry
        from obscura.parity.tool_middleware import ToolRecordReplayMiddleware

        hooks = self._hooks or HookRegistry()

        # Install middleware based on tool_mode
        middleware: ToolRecordReplayMiddleware | None = None
        if spec.tool_mode in ("record", "replay"):
            middleware = ToolRecordReplayMiddleware(
                mode=spec.tool_mode,
                fixtures_dir=spec.fixtures_dir,
            )
            middleware.install(hooks)

        agent_loop = AgentLoop(
            self._backend,
            self._tool_registry,
            max_turns=self._max_turns,
            hooks=hooks,
        )

        # Determine initial prompt from steps or fallback
        initial_prompt = ""
        for step in spec.steps:
            if step.kind.value == "user_prompt":
                initial_prompt = step.text
                break
        if not initial_prompt:
            initial_prompt = spec.title

        collected_events: list[AgentEvent] = []
        passed = True
        details = ""

        try:
            async for event in agent_loop.run(initial_prompt):
                collected_events.append(event)

            # Validate assertions from steps
            for step in spec.steps:
                if step.kind.value == "assert_event":
                    found = any(
                        e.kind.value == step.expected_event
                        for e in collected_events
                    )
                    if not found:
                        passed = False
                        details += f"Missing event: {step.expected_event}. "
                elif step.kind.value == "assert_text":
                    found = any(
                        step.text in e.text
                        for e in collected_events
                        if e.kind == AgentEventKind.TEXT_DELTA
                    )
                    if not found:
                        passed = False
                        details += f"Text not found: {step.text!r}. "

        except Exception as exc:
            passed = False
            details = f"Execution error: {exc}"

        # Flush recorded fixtures
        if middleware is not None:
            middleware.flush()

        observed = tuple(e.kind.value for e in collected_events)
        return ScenarioResult(
            scenario_id=spec.id,
            backend=spec.backend,
            passed=passed,
            observed_events=observed,
            details=details.strip(),
        )
