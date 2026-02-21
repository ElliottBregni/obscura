"""Parity package: semantic conformance models, runner, scoring, reporting."""

from sdk.parity.features import FEATURES
from sdk.parity.models import (
    BackendParityProfile,
    BackendParityScore,
    FeatureStatus,
    ParityFeature,
    ParityReport,
    ScenarioExpectation,
    ScenarioResult,
    ScenarioSpec,
)
from sdk.parity.profiles import PROFILES
from sdk.parity.report import to_markdown
from sdk.parity.runner import run_scenarios
from sdk.parity.scoring import (
    DEFAULT_THRESHOLD_PERCENT,
    backend_percent,
    parity_percent,
    score_backend,
    score_report,
)

__all__ = [
    "FEATURES",
    "PROFILES",
    "BackendParityProfile",
    "BackendParityScore",
    "FeatureStatus",
    "ParityFeature",
    "ParityReport",
    "ScenarioExpectation",
    "ScenarioResult",
    "ScenarioSpec",
    "run_scenarios",
    "DEFAULT_THRESHOLD_PERCENT",
    "backend_percent",
    "parity_percent",
    "score_backend",
    "score_report",
    "to_markdown",
]
