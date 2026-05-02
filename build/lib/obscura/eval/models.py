"""Frozen dataclass models for eval results and compiled eval cases."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class AssertionResult(StrEnum):
    """Outcome of one deterministic assertion."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


class EvalVerdict(StrEnum):
    """Overall verdict for an eval case."""

    PASS = "pass"
    FAIL = "fail"
    REGRESSION = "regression"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Compiled eval models (frozen, produced by EvalCompiler)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledExpectedToolCall:
    """Frozen expected tool call specification."""

    name: str
    order: int | None = None
    args_contain: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CompiledAssertion:
    """Frozen deterministic assertion."""

    kind: str
    turn: int | None = None
    expected: tuple[str, ...] = ()
    substring: str = ""


@dataclass(frozen=True)
class CompiledEvalCase:
    """Frozen eval case ready for execution."""

    id: str
    title: str
    prompt: str
    suite_id: str
    backend: str
    model: str
    max_turns: int = 10
    tool_mode: str = "live"
    fixtures_dir: str = ""
    golden_session_id: str = ""
    tags: tuple[str, ...] = ()
    expect_tool_calls: tuple[CompiledExpectedToolCall, ...] = ()
    assertions: tuple[CompiledAssertion, ...] = ()
    judge_criteria: str = ""
    judge_rubric: str = ""
    judge_pass_threshold: float = 3.0
    regression_baseline_run_id: str = ""
    regression_score_threshold: float = 0.80
    regression_max_score_delta: float = -0.10


# ---------------------------------------------------------------------------
# Result models (frozen, produced by EvalEngine / scoring pipeline)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssertionOutcome:
    """Outcome of one deterministic assertion check."""

    assertion_kind: str
    result: AssertionResult
    message: str = ""


@dataclass(frozen=True)
class JudgeScore:
    """LLM-as-judge scoring result."""

    score: float
    reasoning: str
    criteria: str


@dataclass(frozen=True)
class ToolCallRecord:
    """One observed tool call from an eval run."""

    turn: int
    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict[str, Any])
    tool_result: str = ""
    is_error: bool = False
    latency_ms: int = 0


@dataclass(frozen=True)
class EvalCaseResult:
    """Result of running one eval case."""

    case_id: str
    suite_id: str
    run_id: str
    verdict: EvalVerdict
    deterministic_score: float  # 0.0-1.0, fraction of assertions passed
    judge_score: float | None = None  # 1.0-5.0 Likert if judge configured
    composite_score: float = 0.0  # weighted combination
    assertion_outcomes: tuple[AssertionOutcome, ...] = ()
    judge_detail: JudgeScore | None = None
    tool_calls_observed: tuple[ToolCallRecord, ...] = ()
    output_text: str = ""
    turns_used: int = 0
    latency_ms: int = 0
    error: str = ""
    events: tuple[str, ...] = ()  # AgentEventKind values
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class EvalRunSummary:
    """Aggregated summary of one eval run."""

    run_id: str
    suite_id: str
    backend: str
    model: str
    total_cases: int
    passed: int
    failed: int
    regressions: int
    errors: int
    avg_deterministic_score: float
    avg_judge_score: float | None
    avg_composite_score: float
    case_results: tuple[EvalCaseResult, ...] = ()
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class RegressionComparison:
    """Comparison between current run and baseline."""

    case_id: str
    current_score: float
    baseline_score: float
    delta: float
    is_regression: bool
    details: str = ""
