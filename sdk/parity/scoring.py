"""Scoring helpers for backend semantic parity."""

from __future__ import annotations

from sdk.internal.types import Backend
from sdk.parity.features import feature_map
from sdk.parity.models import (
    BackendConformance,
    BackendParityProfile,
    BackendParityScore,
    FeatureStatus,
    ParityReport,
)


def _status_multiplier(status: FeatureStatus) -> float:
    if status == FeatureStatus.SUPPORTED:
        return 1.0
    if status == FeatureStatus.PARTIAL:
        return 0.5
    return 0.0


def score_backend(profile: BackendParityProfile) -> BackendParityScore:
    """Score one backend profile against global feature weights."""
    fmap = feature_map()
    max_score = 0.0
    score = 0.0
    for support in profile.supports:
        feature = fmap.get(support.feature_id)
        if feature is None:
            continue
        max_score += feature.weight
        score += feature.weight * _status_multiplier(support.status)
    return BackendParityScore(
        backend=profile.backend,
        score=score,
        max_score=max_score,
    )


def score_report(profiles: tuple[BackendParityProfile, ...]) -> ParityReport:
    """Compute aggregated parity score report."""
    return score_report_with_conformance(profiles, ())


def score_report_with_conformance(
    profiles: tuple[BackendParityProfile, ...],
    conformance: tuple[BackendConformance, ...],
) -> ParityReport:
    """Compute parity score report with optional method-level conformance."""
    scores = tuple(score_backend(p) for p in profiles)
    overall_score = sum(s.score for s in scores)
    overall_max = sum(s.max_score for s in scores)

    risks: list[str] = []
    partial_or_worse = 0
    for p in profiles:
        for s in p.supports:
            if s.status != FeatureStatus.SUPPORTED:
                partial_or_worse += 1
    if partial_or_worse:
        risks.append(
            f"{partial_or_worse} feature declarations are partial or unsupported across backends."
        )

    for item in conformance:
        if item.percent < 100.0:
            risks.append(
                f"{item.backend.value} method conformance is {item.percent:.1f}% ({item.passed}/{item.total})."
            )

    return ParityReport(
        backend_scores=scores,
        overall_score=overall_score,
        overall_max=overall_max,
        backend_conformance=conformance,
        residual_risks=tuple(risks),
    )


def parity_percent(report: ParityReport) -> float:
    """Convert a report into a 0-100 percentage."""
    if report.overall_max == 0:
        return 0.0
    return (report.overall_score / report.overall_max) * 100.0


def backend_percent(report: ParityReport, backend: Backend) -> float:
    """Percent score for one backend."""
    for score in report.backend_scores:
        if score.backend == backend:
            if score.max_score == 0:
                return 0.0
            return (score.score / score.max_score) * 100.0
    return 0.0


DEFAULT_THRESHOLD_PERCENT = 79.0
