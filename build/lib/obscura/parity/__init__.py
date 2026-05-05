"""Parity package: semantic conformance models, runner, scoring, reporting."""

from obscura.core.enums.lifecycle import FeatureStatus
from obscura.parity.conformance import evaluate_backend_conformance
from obscura.parity.contracts import CONTRACTS
from obscura.parity.defaults import default_backend_conformance
from obscura.parity.features import FEATURES
from obscura.parity.models import (
    BackendConformance,
    BackendParityProfile,
    BackendParityScore,
    ContractCheckResult,
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
    "CONTRACTS",
    "DEFAULT_THRESHOLD_PERCENT",
    "FEATURES",
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
    "backend_percent",
    "default_backend_conformance",
    "evaluate_backend_conformance",
    "parity_percent",
    "run_scenarios",
    "score_backend",
    "score_report",
    "score_report_with_conformance",
    "to_markdown",
]
