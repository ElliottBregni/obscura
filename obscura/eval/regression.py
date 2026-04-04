"""Regression detection by comparing eval results against baselines."""

from __future__ import annotations

from typing import TYPE_CHECKING

from obscura.eval.models import EvalCaseResult, EvalRunSummary, RegressionComparison

if TYPE_CHECKING:
    from obscura.eval.store import EvalResultStore


async def compare_against_baseline(
    current: EvalCaseResult,
    store: EvalResultStore,
) -> RegressionComparison | None:
    """Compare a case result against its stored baseline.

    Returns ``None`` if no baseline exists (first run).
    """
    baseline = await store.get_baseline(current.case_id, current.suite_id)
    if baseline is None:
        return None

    _baseline_run_id, baseline_score = baseline
    delta = current.composite_score - baseline_score
    is_regression = delta < -abs(0.10)  # default threshold

    return RegressionComparison(
        case_id=current.case_id,
        current_score=current.composite_score,
        baseline_score=baseline_score,
        delta=delta,
        is_regression=is_regression,
        details=f"delta={delta:+.3f} (baseline run: {_baseline_run_id})",
    )


async def compare_with_threshold(
    current: EvalCaseResult,
    store: EvalResultStore,
    *,
    score_threshold: float = 0.80,
    max_score_delta: float = -0.10,
) -> RegressionComparison | None:
    """Compare with explicit regression thresholds from the eval case config.

    A regression is detected when:
    1. ``composite_score < score_threshold`` (absolute floor), OR
    2. ``delta < max_score_delta`` (relative decline from baseline)
    """
    baseline = await store.get_baseline(current.case_id, current.suite_id)
    if baseline is None:
        # No baseline — check absolute threshold only
        is_regression = current.composite_score < score_threshold
        return RegressionComparison(
            case_id=current.case_id,
            current_score=current.composite_score,
            baseline_score=0.0,
            delta=0.0,
            is_regression=is_regression,
            details="No baseline; checked absolute threshold only",
        )

    _baseline_run_id, baseline_score = baseline
    delta = current.composite_score - baseline_score
    below_floor = current.composite_score < score_threshold
    relative_regression = delta < max_score_delta
    is_regression = below_floor or relative_regression

    details_parts: list[str] = []
    if below_floor:
        details_parts.append(
            f"score {current.composite_score:.3f} < threshold {score_threshold}",
        )
    if relative_regression:
        details_parts.append(
            f"delta {delta:+.3f} < max_delta {max_score_delta}",
        )

    return RegressionComparison(
        case_id=current.case_id,
        current_score=current.composite_score,
        baseline_score=baseline_score,
        delta=delta,
        is_regression=is_regression,
        details="; ".join(details_parts) if details_parts else "passed",
    )


async def detect_regressions(
    summary: EvalRunSummary,
    store: EvalResultStore,
) -> list[RegressionComparison]:
    """Check all case results in a run for regressions.

    Returns a list of ``RegressionComparison`` for cases that have baselines.
    """
    comparisons: list[RegressionComparison] = []
    for result in summary.case_results:
        comparison = await compare_against_baseline(result, store)
        if comparison is not None:
            comparisons.append(comparison)
    return comparisons
