"""Tests for obscura.core.cost_tracker."""

from __future__ import annotations

from obscura.core.cost_tracker import CostTracker


def test_record_and_total() -> None:
    t = CostTracker()
    t.record(1000, 500, "claude-sonnet-4-5")
    assert t.turn_count() == 1
    assert t.total_input_tokens() == 1000
    assert t.total_output_tokens() == 500
    assert t.session_total_usd() > 0


def test_pricing_lookup() -> None:
    t = CostTracker()
    turn = t.record(1000, 1000, "claude-sonnet-4-5")
    # sonnet: $0.003/1k input, $0.015/1k output
    expected = (1000 / 1000) * 0.003 + (1000 / 1000) * 0.015
    assert abs(turn.cost_usd - expected) < 0.0001


def test_prefix_match_pricing() -> None:
    t = CostTracker()
    turn = t.record(1000, 1000, "claude-sonnet-4-5-20260101")
    assert turn.cost_usd > 0  # Should match claude-sonnet-4-5 prefix


def test_default_pricing() -> None:
    t = CostTracker()
    turn = t.record(1000, 1000, "unknown-model-xyz")
    assert turn.cost_usd > 0  # Falls back to default


def test_breakdown() -> None:
    t = CostTracker()
    t.record(100, 50, "gpt-4o")
    t.record(200, 100, "claude-sonnet-4-5")
    bd = t.breakdown()
    assert len(bd) == 2
    assert bd[0]["turn"] == 1
    assert bd[1]["turn"] == 2


def test_summary_format() -> None:
    t = CostTracker()
    t.record(1000, 500, "claude-sonnet-4-5")
    s = t.summary()
    assert "1 turns" in s
    assert "1,000 input" in s
    assert "$" in s


def test_reset() -> None:
    t = CostTracker()
    t.record(100, 50, "gpt-4o")
    t.reset()
    assert t.turn_count() == 0
    assert t.session_total_usd() == 0.0
