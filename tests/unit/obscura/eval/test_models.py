"""Tests for eval frozen dataclass models."""

from __future__ import annotations

import pytest

from obscura.eval.models import (
    AssertionOutcome,
    AssertionResult,
    CompiledAssertion,
    CompiledEvalCase,
    CompiledExpectedToolCall,
    EvalCaseResult,
    EvalRunSummary,
    EvalVerdict,
    JudgeScore,
    RegressionComparison,
    ToolCallRecord,
)


class TestEnums:
    def test_assertion_result_values(self) -> None:
        assert AssertionResult.PASS.value == "pass"
        assert AssertionResult.FAIL.value == "fail"
        assert AssertionResult.SKIP.value == "skip"

    def test_eval_verdict_values(self) -> None:
        assert EvalVerdict.PASS.value == "pass"
        assert EvalVerdict.FAIL.value == "fail"
        assert EvalVerdict.REGRESSION.value == "regression"
        assert EvalVerdict.ERROR.value == "error"


class TestFrozenDataclasses:
    def test_compiled_assertion_immutable(self) -> None:
        a = CompiledAssertion(kind="tool_name_match", expected=("read_file",))
        with pytest.raises(AttributeError):
            a.kind = "other"  # type: ignore[misc]

    def test_compiled_expected_tool_call(self) -> None:
        tc = CompiledExpectedToolCall(
            name="bash",
            order=1,
            args_contain=(("cmd", "ls"),),
        )
        assert tc.name == "bash"
        assert tc.args_contain[0] == ("cmd", "ls")

    def test_compiled_eval_case_defaults(self) -> None:
        c = CompiledEvalCase(
            id="c1",
            title="Test",
            prompt="Hello",
            suite_id="s1",
            backend="claude",
            model="sonnet",
        )
        assert c.max_turns == 10
        assert c.tool_mode == "live"
        assert c.assertions == ()
        assert c.judge_criteria == ""

    def test_tool_call_record(self) -> None:
        tc = ToolCallRecord(turn=1, tool_name="bash", tool_input={"cmd": "ls"})
        assert tc.tool_input["cmd"] == "ls"
        assert tc.is_error is False

    def test_assertion_outcome(self) -> None:
        ao = AssertionOutcome(
            assertion_kind="tool_name_match",
            result=AssertionResult.PASS,
            message="Found tool",
        )
        assert ao.result == AssertionResult.PASS

    def test_judge_score(self) -> None:
        js = JudgeScore(score=4.5, reasoning="Good work", criteria="Quality")
        assert js.score == 4.5

    def test_eval_case_result(self) -> None:
        r = EvalCaseResult(
            case_id="c1",
            suite_id="s1",
            run_id="r1",
            verdict=EvalVerdict.PASS,
            deterministic_score=1.0,
        )
        assert r.composite_score == 0.0
        assert r.judge_score is None
        assert r.tool_calls_observed == ()

    def test_eval_run_summary(self) -> None:
        s = EvalRunSummary(
            run_id="r1",
            suite_id="s1",
            backend="claude",
            model="sonnet",
            total_cases=3,
            passed=2,
            failed=1,
            regressions=0,
            errors=0,
            avg_deterministic_score=0.8,
            avg_judge_score=None,
            avg_composite_score=0.8,
        )
        assert s.total_cases == 3
        assert s.case_results == ()

    def test_regression_comparison(self) -> None:
        rc = RegressionComparison(
            case_id="c1",
            current_score=0.7,
            baseline_score=0.9,
            delta=-0.2,
            is_regression=True,
            details="score dropped",
        )
        assert rc.is_regression is True
        assert rc.delta == -0.2
