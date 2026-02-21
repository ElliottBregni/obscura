from __future__ import annotations

from sdk.parity.profiles import PROFILES
from sdk.parity.scoring import DEFAULT_THRESHOLD_PERCENT, parity_percent, score_report


def test_semantic_parity_threshold() -> None:
    """CI-style gate for declared semantic parity progress."""
    report = score_report(PROFILES)
    assert parity_percent(report) >= DEFAULT_THRESHOLD_PERCENT
