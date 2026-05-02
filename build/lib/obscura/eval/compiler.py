"""Compile Pydantic eval specs into frozen dataclass models."""

from __future__ import annotations

from typing import TYPE_CHECKING

from obscura.eval.loader import load_eval_suite
from obscura.eval.models import (
    CompiledAssertion,
    CompiledEvalCase,
    CompiledExpectedToolCall,
)

if TYPE_CHECKING:
    from pathlib import Path

    from obscura.eval.specs import (
        EvalAssertion,
        EvalCaseSpec,
        EvalExpectedToolCall,
        EvalSuiteSpec,
    )


def _compile_expected_tool_call(
    spec: EvalExpectedToolCall,
) -> CompiledExpectedToolCall:
    """Convert an EvalExpectedToolCall to frozen form."""
    return CompiledExpectedToolCall(
        name=spec.name,
        order=spec.order,
        args_contain=tuple((str(k), str(v)) for k, v in spec.args_contain.items()),
    )


def _compile_assertion(spec: EvalAssertion) -> CompiledAssertion:
    """Convert an EvalAssertion to frozen form."""
    parsed = spec
    expected_raw = parsed.expected
    if isinstance(expected_raw, str):
        expected = (expected_raw,) if expected_raw else ()
    else:
        expected = tuple(expected_raw)

    return CompiledAssertion(
        kind=parsed.kind,
        turn=parsed.turn,
        expected=expected,
        substring=parsed.substring,
    )


def compile_case(
    case: EvalCaseSpec,
    suite_id: str,
    default_backend: str,
    default_model: str,
) -> CompiledEvalCase:
    """Compile a single eval case, resolving suite-level defaults."""
    backend = case.backend or default_backend
    model = case.model or default_model

    expect_tool_calls = tuple(
        _compile_expected_tool_call(tc) for tc in case.expect_tool_calls
    )
    assertions = tuple(_compile_assertion(a) for a in case.assertions)

    judge_criteria = ""
    judge_rubric = ""
    judge_pass_threshold = 3.0
    if case.judge is not None:
        judge_criteria = case.judge.criteria
        judge_rubric = case.judge.rubric
        judge_pass_threshold = case.judge.pass_threshold

    regression_baseline_run_id = ""
    regression_score_threshold = 0.80
    regression_max_score_delta = -0.10
    if case.regression is not None:
        regression_baseline_run_id = case.regression.baseline_run_id
        regression_score_threshold = case.regression.score_threshold
        regression_max_score_delta = case.regression.max_score_delta

    return CompiledEvalCase(
        id=case.id,
        title=case.title,
        prompt=case.prompt,
        suite_id=suite_id,
        backend=backend,
        model=model,
        max_turns=case.max_turns,
        tool_mode=case.tool_mode,
        fixtures_dir=case.fixtures_dir,
        golden_session_id=case.golden_session_id,
        tags=tuple(case.tags),
        expect_tool_calls=expect_tool_calls,
        assertions=assertions,
        judge_criteria=judge_criteria,
        judge_rubric=judge_rubric,
        judge_pass_threshold=judge_pass_threshold,
        regression_baseline_run_id=regression_baseline_run_id,
        regression_score_threshold=regression_score_threshold,
        regression_max_score_delta=regression_max_score_delta,
    )


def compile_suite(spec: EvalSuiteSpec) -> tuple[CompiledEvalCase, ...]:
    """Compile an entire eval suite into frozen cases."""
    suite_id = spec.meta.id
    default_backend = spec.meta.backend or ""
    default_model = spec.meta.model or ""

    return tuple(
        compile_case(case, suite_id, default_backend, default_model)
        for case in spec.cases
    )


def compile_suite_from_path(path: Path) -> tuple[CompiledEvalCase, ...]:
    """Load and compile an eval suite from a TOML file path."""
    spec = load_eval_suite(path)
    return compile_suite(spec)
