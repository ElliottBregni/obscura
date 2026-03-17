"""Scoring pipeline: deterministic assertions and LLM-as-judge."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from obscura.eval.models import (
    AssertionOutcome,
    AssertionResult,
    CompiledAssertion,
    CompiledEvalCase,
    JudgeScore,
    ToolCallRecord,
)

if TYPE_CHECKING:
    from obscura.core.types import BackendProtocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Deterministic assertion checkers
# ---------------------------------------------------------------------------


def _check_tool_name_match(
    assertion: CompiledAssertion,
    tool_calls: tuple[ToolCallRecord, ...],
) -> AssertionOutcome:
    """Check that a specific tool was called on the given turn."""
    expected_name = assertion.expected[0] if assertion.expected else ""
    if not expected_name:
        return AssertionOutcome(
            assertion_kind=assertion.kind,
            result=AssertionResult.SKIP,
            message="No expected tool name specified",
        )

    for tc in tool_calls:
        if assertion.turn is not None and tc.turn != assertion.turn:
            continue
        if tc.tool_name == expected_name:
            return AssertionOutcome(
                assertion_kind=assertion.kind,
                result=AssertionResult.PASS,
                message=f"Tool '{expected_name}' called on turn {tc.turn}",
            )

    return AssertionOutcome(
        assertion_kind=assertion.kind,
        result=AssertionResult.FAIL,
        message=f"Tool '{expected_name}' not called"
        + (f" on turn {assertion.turn}" if assertion.turn is not None else ""),
    )


def _check_output_contains(
    assertion: CompiledAssertion,
    output_text: str,
) -> AssertionOutcome:
    """Check that output text contains a substring."""
    if not assertion.substring:
        return AssertionOutcome(
            assertion_kind=assertion.kind,
            result=AssertionResult.SKIP,
            message="No substring specified",
        )

    if assertion.substring in output_text:
        return AssertionOutcome(
            assertion_kind=assertion.kind,
            result=AssertionResult.PASS,
            message=f"Output contains '{assertion.substring}'",
        )

    return AssertionOutcome(
        assertion_kind=assertion.kind,
        result=AssertionResult.FAIL,
        message=f"Output does not contain '{assertion.substring}'",
    )


def _check_tool_sequence(
    assertion: CompiledAssertion,
    tool_calls: tuple[ToolCallRecord, ...],
) -> AssertionOutcome:
    """Check that tools were called in the expected order."""
    expected = list(assertion.expected)
    observed = [tc.tool_name for tc in tool_calls]

    if not expected:
        return AssertionOutcome(
            assertion_kind=assertion.kind,
            result=AssertionResult.SKIP,
            message="No expected sequence specified",
        )

    # Check that expected is a subsequence of observed
    idx = 0
    for name in observed:
        if idx < len(expected) and name == expected[idx]:
            idx += 1
    if idx == len(expected):
        return AssertionOutcome(
            assertion_kind=assertion.kind,
            result=AssertionResult.PASS,
            message=f"Tool sequence matched: {expected}",
        )

    return AssertionOutcome(
        assertion_kind=assertion.kind,
        result=AssertionResult.FAIL,
        message=f"Expected sequence {expected}, observed {observed}",
    )


def _check_no_tool_calls(
    assertion: CompiledAssertion,
    tool_calls: tuple[ToolCallRecord, ...],
) -> AssertionOutcome:
    """Check that no tool calls were made."""
    if not tool_calls:
        return AssertionOutcome(
            assertion_kind=assertion.kind,
            result=AssertionResult.PASS,
            message="No tool calls made",
        )

    names = [tc.tool_name for tc in tool_calls]
    return AssertionOutcome(
        assertion_kind=assertion.kind,
        result=AssertionResult.FAIL,
        message=f"Expected no tool calls, but got: {names}",
    )


def _check_event_present(
    assertion: CompiledAssertion,
    events: tuple[str, ...],
) -> AssertionOutcome:
    """Check that a specific event kind was emitted."""
    expected_event = assertion.expected[0] if assertion.expected else ""
    if not expected_event:
        return AssertionOutcome(
            assertion_kind=assertion.kind,
            result=AssertionResult.SKIP,
            message="No expected event specified",
        )

    if expected_event in events:
        return AssertionOutcome(
            assertion_kind=assertion.kind,
            result=AssertionResult.PASS,
            message=f"Event '{expected_event}' present",
        )

    return AssertionOutcome(
        assertion_kind=assertion.kind,
        result=AssertionResult.FAIL,
        message=f"Event '{expected_event}' not found in {list(events)}",
    )


def _check_arg_exact_match(
    assertion: CompiledAssertion,
    tool_calls: tuple[ToolCallRecord, ...],
) -> AssertionOutcome:
    """Check that a tool call has exact argument values.

    Expected format: ``["tool_name", "arg_key", "arg_value"]``.
    """
    if len(assertion.expected) < 3:  # noqa: PLR2004
        return AssertionOutcome(
            assertion_kind=assertion.kind,
            result=AssertionResult.SKIP,
            message="Expected format: [tool_name, arg_key, arg_value]",
        )

    tool_name, arg_key, arg_value = (
        assertion.expected[0],
        assertion.expected[1],
        assertion.expected[2],
    )

    for tc in tool_calls:
        if tc.tool_name != tool_name:
            continue
        actual = tc.tool_input.get(arg_key)
        if str(actual) == arg_value:
            return AssertionOutcome(
                assertion_kind=assertion.kind,
                result=AssertionResult.PASS,
                message=f"{tool_name}.{arg_key} == '{arg_value}'",
            )

    return AssertionOutcome(
        assertion_kind=assertion.kind,
        result=AssertionResult.FAIL,
        message=f"No call to '{tool_name}' with {arg_key}='{arg_value}'",
    )


_ASSERTION_CHECKERS: dict[
    str,
    Any,
] = {
    "tool_name_match": _check_tool_name_match,
    "output_contains": _check_output_contains,
    "tool_sequence": _check_tool_sequence,
    "no_tool_calls": _check_no_tool_calls,
    "event_present": _check_event_present,
    "arg_exact_match": _check_arg_exact_match,
}


# ---------------------------------------------------------------------------
# Deterministic scoring
# ---------------------------------------------------------------------------


def score_deterministic(
    case: CompiledEvalCase,
    events: tuple[str, ...],
    output_text: str,
    tool_calls: tuple[ToolCallRecord, ...],
) -> tuple[float, tuple[AssertionOutcome, ...]]:
    """Score a case using deterministic assertions.

    Returns ``(score, outcomes)`` where score is 0.0-1.0.
    """
    outcomes: list[AssertionOutcome] = []

    # Check explicit assertions
    for assertion in case.assertions:
        checker = _ASSERTION_CHECKERS.get(assertion.kind)
        if checker is None:
            outcomes.append(
                AssertionOutcome(
                    assertion_kind=assertion.kind,
                    result=AssertionResult.SKIP,
                    message=f"Unknown assertion kind: {assertion.kind}",
                )
            )
            continue

        # Route to appropriate checker based on signature
        if assertion.kind in ("tool_name_match", "tool_sequence", "no_tool_calls"):
            outcome = checker(assertion, tool_calls)
        elif assertion.kind == "output_contains":
            outcome = checker(assertion, output_text)
        elif assertion.kind == "event_present":
            outcome = checker(assertion, events)
        elif assertion.kind == "arg_exact_match":
            outcome = checker(assertion, tool_calls)
        else:
            outcome = AssertionOutcome(
                assertion_kind=assertion.kind,
                result=AssertionResult.SKIP,
                message=f"Unhandled assertion kind: {assertion.kind}",
            )
        outcomes.append(outcome)

    # Check expected tool calls
    for etc in case.expect_tool_calls:
        found = False
        for tc in tool_calls:
            if tc.tool_name == etc.name:
                # Verify args_contain
                args_ok = all(
                    str(tc.tool_input.get(k)) == v for k, v in etc.args_contain
                )
                if args_ok:
                    found = True
                    break

        outcomes.append(
            AssertionOutcome(
                assertion_kind="expect_tool_call",
                result=AssertionResult.PASS if found else AssertionResult.FAIL,
                message=f"Expected tool '{etc.name}'"
                + (" found" if found else " not found"),
            )
        )

    total = len(outcomes)
    if total == 0:
        return 1.0, ()

    passed = sum(1 for o in outcomes if o.result == AssertionResult.PASS)
    skipped = sum(1 for o in outcomes if o.result == AssertionResult.SKIP)
    effective_total = total - skipped
    score = passed / effective_total if effective_total > 0 else 1.0

    return score, tuple(outcomes)


# ---------------------------------------------------------------------------
# LLM-as-judge scoring
# ---------------------------------------------------------------------------

_JUDGE_PROMPT_TEMPLATE = """You are evaluating an AI agent's performance on a task.

## Task
{prompt}

## Agent Output
{output_text}

## Tool Calls Made
{tool_calls_text}

## Evaluation Criteria
{criteria}

## Rubric
{rubric}

Rate the agent's performance on a scale of 1-5 according to the rubric above.
Respond with ONLY a JSON object: {{"score": <int 1-5>, "reasoning": "<explanation>"}}"""


def _format_tool_calls(tool_calls: tuple[ToolCallRecord, ...]) -> str:
    """Format tool calls for the judge prompt."""
    if not tool_calls:
        return "(no tool calls)"
    lines: list[str] = []
    for tc in tool_calls:
        args_str = json.dumps(tc.tool_input, indent=2) if tc.tool_input else "{}"
        result_preview = tc.tool_result[:200] if tc.tool_result else "(no result)"
        lines.append(
            f"Turn {tc.turn}: {tc.tool_name}({args_str})"
            f"\n  → {'ERROR: ' if tc.is_error else ''}{result_preview}"
        )
    return "\n".join(lines)


async def score_with_judge(
    case: CompiledEvalCase,
    output_text: str,
    tool_calls: tuple[ToolCallRecord, ...],
    judge_backend: BackendProtocol,
) -> JudgeScore:
    """Score a case using an LLM judge.

    Returns a ``JudgeScore`` with the 1-5 Likert rating and reasoning.
    """
    prompt = _JUDGE_PROMPT_TEMPLATE.format(
        prompt=case.prompt,
        output_text=output_text[:2000],
        tool_calls_text=_format_tool_calls(tool_calls),
        criteria=case.judge_criteria,
        rubric=case.judge_rubric or "(no rubric provided)",
    )

    try:
        message = await judge_backend.send(prompt)
        response_text = ""
        for block in message.content:
            if block.kind == "text":
                response_text += block.text

        parsed = json.loads(response_text)
        score = float(parsed.get("score", 3))
        reasoning = str(parsed.get("reasoning", ""))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("Judge response parse failed: %s", exc)
        score = 3.0
        reasoning = f"Parse error: {exc}"
    except Exception as exc:
        logger.warning("Judge call failed: %s", exc)
        score = 3.0
        reasoning = f"Judge error: {exc}"

    return JudgeScore(
        score=max(1.0, min(5.0, score)),
        reasoning=reasoning,
        criteria=case.judge_criteria,
    )


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------


def compute_composite(
    deterministic: float,
    judge: float | None,
    *,
    det_weight: float = 0.6,
    judge_weight: float = 0.4,
) -> float:
    """Combine deterministic and judge scores into a composite 0.0-1.0 score.

    If no judge score is available, returns the deterministic score only.
    Judge scores (1-5 Likert) are normalized to 0.0-1.0 before weighting.
    """
    if judge is None:
        return deterministic
    normalized_judge = (judge - 1.0) / 4.0  # map 1-5 to 0.0-1.0
    return det_weight * deterministic + judge_weight * normalized_judge
