from __future__ import annotations

from obscura.core.types import Backend
from obscura.parity.models import BackendParityProfile, FeatureStatus, FeatureSupport
from obscura.parity.profiles import PROFILES
from obscura.parity.scoring import (
    DEFAULT_THRESHOLD_PERCENT,
    backend_percent,
    parity_percent,
    score_report,
)


def test_score_report_outputs_percent() -> None:
    report = score_report(PROFILES)
    pct = parity_percent(report)
    assert pct > 0.0
    assert pct <= 100.0


def test_backend_percent_openai() -> None:
    report = score_report(PROFILES)
    pct = backend_percent(report, Backend.OPENAI)
    assert pct > 0.0


def test_threshold_constant() -> None:
    assert DEFAULT_THRESHOLD_PERCENT == 79.0


def test_status_weights() -> None:
    profile = BackendParityProfile(
        backend=Backend.OPENAI,
        supports=(
            FeatureSupport("stream_text", FeatureStatus.SUPPORTED),
            FeatureSupport("stream_tool_lifecycle", FeatureStatus.PARTIAL),
        ),
    )
    report = score_report((profile,))
    pct = parity_percent(report)
    assert 70.0 <= pct <= 80.0
