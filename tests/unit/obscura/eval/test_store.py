"""Tests for eval result store."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from obscura.eval.models import (
    AssertionOutcome,
    AssertionResult,
    EvalCaseResult,
    EvalRunSummary,
    EvalVerdict,
    JudgeScore,
    ToolCallRecord,
)
from obscura.eval.store import EvalResultStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def store(tmp_path: Path) -> EvalResultStore:
    return EvalResultStore(db_path=tmp_path / "test_results.db")


def _make_case_result(
    case_id: str = "c1",
    run_id: str = "r1",
    verdict: EvalVerdict = EvalVerdict.PASS,
    composite_score: float = 0.9,
) -> EvalCaseResult:
    return EvalCaseResult(
        case_id=case_id,
        suite_id="test-suite",
        run_id=run_id,
        verdict=verdict,
        deterministic_score=0.9,
        judge_score=4.0,
        composite_score=composite_score,
        assertion_outcomes=(
            AssertionOutcome(
                assertion_kind="tool_name_match",
                result=AssertionResult.PASS,
                message="found",
            ),
        ),
        judge_detail=JudgeScore(score=4.0, reasoning="Good", criteria="Quality"),
        tool_calls_observed=(
            ToolCallRecord(turn=1, tool_name="bash", tool_input={"cmd": "ls"}),
        ),
        output_text="output text",
        turns_used=2,
        latency_ms=500,
    )


def _make_summary(
    run_id: str = "r1",
    case_results: tuple[EvalCaseResult, ...] | None = None,
) -> EvalRunSummary:
    if case_results is None:
        case_results = (_make_case_result(run_id=run_id),)
    return EvalRunSummary(
        run_id=run_id,
        suite_id="test-suite",
        backend="claude",
        model="sonnet",
        total_cases=len(case_results),
        passed=sum(1 for r in case_results if r.verdict == EvalVerdict.PASS),
        failed=0,
        regressions=0,
        errors=0,
        avg_deterministic_score=0.9,
        avg_judge_score=4.0,
        avg_composite_score=0.9,
        case_results=case_results,
    )


class TestSaveAndRetrieve:
    async def test_save_and_get_case_result(self, store: EvalResultStore) -> None:
        summary = _make_summary()
        await store.save_run(summary)

        result = await store.get_case_result("r1", "c1")
        assert result is not None
        assert result.case_id == "c1"
        assert result.verdict == EvalVerdict.PASS
        assert result.deterministic_score == 0.9
        assert result.judge_score == 4.0
        assert result.composite_score == 0.9

    async def test_tool_calls_roundtrip(self, store: EvalResultStore) -> None:
        summary = _make_summary()
        await store.save_run(summary)

        result = await store.get_case_result("r1", "c1")
        assert result is not None
        assert len(result.tool_calls_observed) == 1
        assert result.tool_calls_observed[0].tool_name == "bash"
        assert result.tool_calls_observed[0].tool_input == {"cmd": "ls"}

    async def test_assertion_outcomes_roundtrip(self, store: EvalResultStore) -> None:
        summary = _make_summary()
        await store.save_run(summary)

        result = await store.get_case_result("r1", "c1")
        assert result is not None
        assert len(result.assertion_outcomes) == 1
        assert result.assertion_outcomes[0].result == AssertionResult.PASS

    async def test_judge_detail_roundtrip(self, store: EvalResultStore) -> None:
        summary = _make_summary()
        await store.save_run(summary)

        result = await store.get_case_result("r1", "c1")
        assert result is not None
        assert result.judge_detail is not None
        assert result.judge_detail.score == 4.0
        assert result.judge_detail.reasoning == "Good"

    async def test_missing_result_returns_none(self, store: EvalResultStore) -> None:
        result = await store.get_case_result("nonexistent", "c1")
        assert result is None


class TestBaselines:
    async def test_promote_and_get_baseline(self, store: EvalResultStore) -> None:
        summary = _make_summary()
        await store.save_run(summary)
        await store.promote_baseline("r1", "test-suite")

        baseline = await store.get_baseline("c1", "test-suite")
        assert baseline is not None
        run_id, score = baseline
        assert run_id == "r1"
        assert score == 0.9

    async def test_no_baseline(self, store: EvalResultStore) -> None:
        baseline = await store.get_baseline("c1", "test-suite")
        assert baseline is None

    async def test_baseline_updates(self, store: EvalResultStore) -> None:
        # First run
        summary1 = _make_summary(run_id="r1")
        await store.save_run(summary1)
        await store.promote_baseline("r1", "test-suite")

        # Second run with different score
        cr2 = _make_case_result(run_id="r2", composite_score=0.95)
        summary2 = _make_summary(run_id="r2", case_results=(cr2,))
        await store.save_run(summary2)
        await store.promote_baseline("r2", "test-suite")

        baseline = await store.get_baseline("c1", "test-suite")
        assert baseline is not None
        assert baseline[0] == "r2"
        assert baseline[1] == 0.95


class TestListOperations:
    async def test_list_runs(self, store: EvalResultStore) -> None:
        await store.save_run(_make_summary(run_id="r1"))
        await store.save_run(_make_summary(run_id="r2"))

        runs = await store.list_runs()
        assert len(runs) == 2

    async def test_list_runs_by_suite(self, store: EvalResultStore) -> None:
        await store.save_run(_make_summary(run_id="r1"))

        runs = await store.list_runs(suite_id="test-suite")
        assert len(runs) == 1

        runs = await store.list_runs(suite_id="other-suite")
        assert len(runs) == 0

    async def test_list_baselines(self, store: EvalResultStore) -> None:
        await store.save_run(_make_summary())
        await store.promote_baseline("r1", "test-suite")

        baselines = await store.list_baselines()
        assert len(baselines) == 1
        assert baselines[0]["case_id"] == "c1"
