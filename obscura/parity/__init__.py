"""Parity package: semantic conformance models, runner, scoring, reporting."""

from obscura.parity.features import FEATURES
from obscura.parity.contracts import CONTRACTS
from obscura.parity.conformance import evaluate_backend_conformance
from obscura.parity.defaults import default_backend_conformance
from obscura.parity.models import (
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
from obscura.parity.profiles import PROFILES
from obscura.parity.report import to_markdown
from obscura.parity.runner import run_scenarios
from obscura.parity.scoring import (
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
