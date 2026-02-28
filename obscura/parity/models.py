"""Typed parity models for backend semantic conformance."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from obscura.core.types import Backend


class FeatureStatus(str, Enum):
    """Parity status for one backend feature."""

    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ParityFeature:
    """A single parity feature definition."""

    id: str
    title: str
    description: str
    weight: float = 1.0


@dataclass(frozen=True)
class FeatureSupport:
    """Support declaration for one feature on one backend."""

    feature_id: str
    status: FeatureStatus
    notes: str = ""


@dataclass(frozen=True)
class BackendParityProfile:
    """Declared parity profile for a backend."""

    backend: Backend
    supports: tuple[FeatureSupport, ...]


class ScenarioStepKind(str, Enum):
    """Kind of scenario step."""

    USER_PROMPT = "user_prompt"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ASSERT_TEXT = "assert_text"
    ASSERT_EVENT = "assert_event"


@dataclass(frozen=True)
class ScenarioStep:
    """One step in a structured scenario sequence."""

    kind: ScenarioStepKind
    text: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())
    tool_result: str = ""
    expected_event: str = ""


@dataclass(frozen=True)
class ScenarioSpec:
    """One reusable parity scenario.

    ``steps`` is an ordered sequence for the AgentLoopScenarioExecutor.
    ``tool_mode`` controls tool middleware behavior:
    - ``"live"``   — tools execute normally
    - ``"record"`` — tools execute and results are saved as fixtures
    - ``"replay"`` — tool calls return saved fixtures (no real execution)
    """

    id: str
    title: str
    feature_ids: tuple[str, ...]
    backend: Backend
    steps: tuple[ScenarioStep, ...] = ()
    tool_mode: str = "live"  # "live" | "record" | "replay"
    fixtures_dir: str = ""


@dataclass(frozen=True)
class ScenarioExpectation:
    """Expected behavior for a scenario."""

    should_pass: bool
    expected_events: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScenarioResult:
    """Observed outcome of a scenario execution."""

    scenario_id: str
    backend: Backend
    passed: bool
    observed_events: tuple[str, ...] = ()
    details: str = ""


@dataclass(frozen=True)
class BackendParityScore:
    """Score summary for one backend."""

    backend: Backend
    score: float
    max_score: float


@dataclass(frozen=True)
class ParityReport:
    """Aggregated parity scoring output."""

    backend_scores: tuple[BackendParityScore, ...]
    overall_score: float
    overall_max: float
    backend_conformance: tuple[BackendConformance, ...] = field(default_factory=tuple)
    residual_risks: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MethodContract:
    """Backend-agnostic contract for one semantic behavior."""

    id: str
    title: str
    required_methods: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    required_native_features: tuple[str, ...] = ()
    applicable_backends: tuple[Backend, ...] = (
        Backend.OPENAI,
        Backend.MOONSHOT,
        Backend.CLAUDE,
        Backend.COPILOT,
        Backend.LOCALLLM,
    )


@dataclass(frozen=True)
class ContractCheckResult:
    """Observed result of one method contract check."""

    backend: Backend
    contract_id: str
    passed: bool
    missing_methods: tuple[str, ...] = ()
    missing_capabilities: tuple[str, ...] = ()
    missing_native_features: tuple[str, ...] = ()


@dataclass(frozen=True)
class BackendConformance:
    """Method-level conformance summary for one backend."""

    backend: Backend
    checks: tuple[ContractCheckResult, ...]

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def percent(self) -> float:
        if self.total == 0:
            return 100.0
        return (self.passed / self.total) * 100.0
