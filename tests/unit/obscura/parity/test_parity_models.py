from __future__ import annotations

from obscura.core.types import Backend
from obscura.parity.models import (
    BackendParityProfile,
    FeatureStatus,
    FeatureSupport,
    ParityFeature,
    ScenarioExpectation,
    ScenarioResult,
    ScenarioSpec,
)


def test_parity_feature_defaults() -> None:
    f = ParityFeature(id="x", title="X", description="desc")
    assert f.weight == 1.0


def test_profile_creation() -> None:
    p = BackendParityProfile(
        backend=Backend.OPENAI,
        supports=(FeatureSupport("stream_text", FeatureStatus.SUPPORTED),),
    )
    assert p.backend is Backend.OPENAI


def test_scenario_objects() -> None:
    spec = ScenarioSpec(
        id="s1",
        title="demo",
        feature_ids=("stream_text",),
        backend=Backend.COPILOT,
    )
    exp = ScenarioExpectation(should_pass=True, expected_events=("x",))
    result = ScenarioResult(
        scenario_id=spec.id,
        backend=spec.backend,
        passed=True,
        observed_events=("x",),
    )
    assert result.passed == exp.should_pass
