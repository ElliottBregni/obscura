"""Parity package: semantic conformance models, runner, scoring, reporting."""

from sdk.parity.features import FEATURES
from sdk.parity.contracts import CONTRACTS
from sdk.parity.conformance import evaluate_backend_conformance
from sdk.parity.defaults import default_backend_conformance
from sdk.parity.models import (
    BackendConformance,
    BackendParityProfile,
    BackendParityScore,
    ContractCheckResult,
    FeatureStatus,
    MethodContract,
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
    score_report_with_conformance,
)

__all__ = [
    "FEATURES",
    "CONTRACTS",
    "PROFILES",
    "BackendConformance",
    "BackendParityProfile",
    "BackendParityScore",
    "ContractCheckResult",
    "FeatureStatus",
    "MethodContract",
    "ParityFeature",
    "ParityReport",
    "ScenarioExpectation",
    "ScenarioResult",
    "ScenarioSpec",
    "run_scenarios",
    "evaluate_backend_conformance",
    "default_backend_conformance",
    "DEFAULT_THRESHOLD_PERCENT",
    "backend_percent",
    "parity_percent",
    "score_backend",
    "score_report",
    "score_report_with_conformance",
    "to_markdown",
]
