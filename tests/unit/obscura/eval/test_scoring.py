"""Tests for the eval scoring pipeline."""

from __future__ import annotations

from obscura.eval.models import (
    AssertionResult,
    CompiledAssertion,
    CompiledEvalCase,
    CompiledExpectedToolCall,
    ToolCallRecord,
)
from obscura.eval.scoring import compute_composite, score_deterministic


def _make_case(
    assertions: tuple[CompiledAssertion, ...] = (),
    expect_tool_calls: tuple[CompiledExpectedToolCall, ...] = (),
) -> CompiledEvalCase:
    return CompiledEvalCase(
        id="test-case",
        title="Test",
        prompt="Do something",
        suite_id="test-suite",
        backend="claude",
        model="sonnet",
        assertions=assertions,
        expect_tool_calls=expect_tool_calls,
    )


class TestToolNameMatch:
    def test_passes_when_tool_called(self) -> None:
        case = _make_case(
            assertions=(
                CompiledAssertion(
                    kind="tool_name_match",
                    expected=("read_file",),
                    turn=1,
                ),
            ),
        )
        tool_calls = (ToolCallRecord(turn=1, tool_name="read_file"),)
        score, outcomes = score_deterministic(case, (), "", tool_calls)
        assert score == 1.0
        assert outcomes[0].result == AssertionResult.PASS

    def test_fails_when_tool_not_called(self) -> None:
        case = _make_case(
            assertions=(
                CompiledAssertion(
                    kind="tool_name_match",
                    expected=("read_file",),
                    turn=1,
                ),
            ),
        )
        score, outcomes = score_deterministic(case, (), "", ())
        assert score == 0.0
        assert outcomes[0].result == AssertionResult.FAIL

    def test_fails_on_wrong_turn(self) -> None:
        case = _make_case(
            assertions=(
                CompiledAssertion(
                    kind="tool_name_match",
                    expected=("read_file",),
                    turn=1,
                ),
            ),
        )
        tool_calls = (ToolCallRecord(turn=2, tool_name="read_file"),)
        score, _outcomes = score_deterministic(case, (), "", tool_calls)
        assert score == 0.0


class TestOutputContains:
    def test_passes(self) -> None:
        case = _make_case(
            assertions=(CompiledAssertion(kind="output_contains", substring="hello"),),
        )
        score, outcomes = score_deterministic(case, (), "hello world", ())
        assert score == 1.0
        assert outcomes[0].result == AssertionResult.PASS

    def test_fails(self) -> None:
        case = _make_case(
            assertions=(CompiledAssertion(kind="output_contains", substring="hello"),),
        )
        score, outcomes = score_deterministic(case, (), "goodbye world", ())
        assert score == 0.0
        assert outcomes[0].result == AssertionResult.FAIL


class TestToolSequence:
    def test_passes_exact(self) -> None:
        case = _make_case(
            assertions=(
                CompiledAssertion(
                    kind="tool_sequence",
                    expected=("read", "write"),
                ),
            ),
        )
        tool_calls = (
            ToolCallRecord(turn=1, tool_name="read"),
            ToolCallRecord(turn=2, tool_name="write"),
        )
        score, _outcomes = score_deterministic(case, (), "", tool_calls)
        assert score == 1.0

    def test_passes_subsequence(self) -> None:
        case = _make_case(
            assertions=(
                CompiledAssertion(
                    kind="tool_sequence",
                    expected=("read", "write"),
                ),
            ),
        )
        tool_calls = (
            ToolCallRecord(turn=1, tool_name="read"),
            ToolCallRecord(turn=2, tool_name="search"),
            ToolCallRecord(turn=3, tool_name="write"),
        )
        score, _outcomes = score_deterministic(case, (), "", tool_calls)
        assert score == 1.0

    def test_fails_wrong_order(self) -> None:
        case = _make_case(
            assertions=(
                CompiledAssertion(
                    kind="tool_sequence",
                    expected=("read", "write"),
                ),
            ),
        )
        tool_calls = (
            ToolCallRecord(turn=1, tool_name="write"),
            ToolCallRecord(turn=2, tool_name="read"),
        )
        score, _outcomes = score_deterministic(case, (), "", tool_calls)
        assert score == 0.0


class TestNoToolCalls:
    def test_passes_empty(self) -> None:
        case = _make_case(
            assertions=(CompiledAssertion(kind="no_tool_calls"),),
        )
        score, outcomes = score_deterministic(case, (), "answer", ())
        assert score == 1.0
        assert outcomes[0].result == AssertionResult.PASS

    def test_fails_with_calls(self) -> None:
        case = _make_case(
            assertions=(CompiledAssertion(kind="no_tool_calls"),),
        )
        tool_calls = (ToolCallRecord(turn=1, tool_name="bash"),)
        score, outcomes = score_deterministic(case, (), "", tool_calls)
        assert score == 0.0
        assert outcomes[0].result == AssertionResult.FAIL


class TestEventPresent:
    def test_passes(self) -> None:
        case = _make_case(
            assertions=(
                CompiledAssertion(kind="event_present", expected=("AGENT_DONE",)),
            ),
        )
        events = ("TURN_START", "TEXT_DELTA", "AGENT_DONE")
        score, _outcomes = score_deterministic(case, events, "", ())
        assert score == 1.0

    def test_fails(self) -> None:
        case = _make_case(
            assertions=(CompiledAssertion(kind="event_present", expected=("ERROR",)),),
        )
        events = ("TURN_START", "AGENT_DONE")
        score, _outcomes = score_deterministic(case, events, "", ())
        assert score == 0.0


class TestExpectToolCalls:
    def test_expected_tool_found(self) -> None:
        case = _make_case(
            expect_tool_calls=(CompiledExpectedToolCall(name="bash"),),
        )
        tool_calls = (ToolCallRecord(turn=1, tool_name="bash"),)
        score, outcomes = score_deterministic(case, (), "", tool_calls)
        assert score == 1.0
        assert any(
            o.assertion_kind == "expect_tool_call" and o.result == AssertionResult.PASS
            for o in outcomes
        )

    def test_expected_tool_with_args(self) -> None:
        case = _make_case(
            expect_tool_calls=(
                CompiledExpectedToolCall(
                    name="read_file",
                    args_contain=(("path", "/tmp/x"),),
                ),
            ),
        )
        tool_calls = (
            ToolCallRecord(
                turn=1,
                tool_name="read_file",
                tool_input={"path": "/tmp/x"},
            ),
        )
        score, _outcomes = score_deterministic(case, (), "", tool_calls)
        assert score == 1.0

    def test_expected_tool_not_found(self) -> None:
        case = _make_case(
            expect_tool_calls=(CompiledExpectedToolCall(name="bash"),),
        )
        score, _outcomes = score_deterministic(case, (), "", ())
        assert score == 0.0


class TestMixedAssertions:
    def test_partial_score(self) -> None:
        case = _make_case(
            assertions=(
                CompiledAssertion(kind="output_contains", substring="hello"),
                CompiledAssertion(kind="output_contains", substring="missing"),
            ),
        )
        score, outcomes = score_deterministic(case, (), "hello world", ())
        assert score == 0.5
        assert outcomes[0].result == AssertionResult.PASS
        assert outcomes[1].result == AssertionResult.FAIL

    def test_empty_assertions_scores_one(self) -> None:
        case = _make_case()
        score, outcomes = score_deterministic(case, (), "", ())
        assert score == 1.0
        assert outcomes == ()


class TestCompositeScore:
    def test_deterministic_only(self) -> None:
        assert compute_composite(0.8, None) == 0.8

    def test_with_judge(self) -> None:
        # det=1.0, judge=5.0 -> 0.6*1.0 + 0.4*((5-1)/4) = 0.6 + 0.4 = 1.0
        assert compute_composite(1.0, 5.0) == 1.0

    def test_with_low_judge(self) -> None:
        # det=1.0, judge=1.0 -> 0.6*1.0 + 0.4*0.0 = 0.6
        assert compute_composite(1.0, 1.0) == 0.6

    def test_mid_range(self) -> None:
        # det=0.5, judge=3.0 -> 0.6*0.5 + 0.4*0.5 = 0.3 + 0.2 = 0.5
        assert compute_composite(0.5, 3.0) == 0.5
