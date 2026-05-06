"""Tests for the reveal-cursor jitter helper.

Streaming text is paced by `_compute_reveal_burst` once per frame. Jitter
makes the visible reveal feel less mechanical — but it must never:

  * stall the cursor (always advance ≥1 char while there's backlog), or
  * over-shoot the buffer (the call site clamps via min(), but we still
    sanity-check the helper never returns 0 for non-empty backlog).

We also assert the *sum* of bursts converges on the backlog within a
bounded number of frames, so jitter can't slow streaming below the base.
"""

from __future__ import annotations

import random

import pytest

from obscura.cli.renderer.reveal import (
    REVEAL_JITTER_HIGH,
    REVEAL_JITTER_LOW,
    compute_reveal_burst,
)

# Aliases preserve the assertion text + private-API "are these
# constants stable?" intent from when the helper lived inside the
# bordered REPL renderer module. The TUI now imports the same helper
# (see ``obscura.cli.renderer.reveal``).
_REVEAL_JITTER_HIGH = REVEAL_JITTER_HIGH
_REVEAL_JITTER_LOW = REVEAL_JITTER_LOW
_compute_reveal_burst = compute_reveal_burst


@pytest.fixture(autouse=True)
def _seed_random() -> None:
    """Deterministic jitter for assertions."""
    random.seed(42)


def test_zero_backlog_returns_zero() -> None:
    assert _compute_reveal_burst(backlog=0, base=12) == 0


def test_positive_backlog_always_advances() -> None:
    """Even with the unluckiest jitter roll, a non-empty backlog must
    never produce a 0-burst (would stall the cursor)."""
    for _ in range(1000):
        burst = _compute_reveal_burst(backlog=5, base=1)
        assert burst >= 1


def test_jitter_stays_within_bounds_relative_to_base() -> None:
    """Without backlog scaling, the jittered burst sits in
    [floor(base * LOW), ceil(base * HIGH)]."""
    base = 100
    samples = [_compute_reveal_burst(backlog=10, base=base) for _ in range(500)]
    lo = max(1, int(base * _REVEAL_JITTER_LOW))
    hi = int(base * _REVEAL_JITTER_HIGH)
    assert min(samples) >= lo
    assert max(samples) <= hi


def test_backlog_scaling_kicks_in_above_thresholds() -> None:
    """Heavy backlog (>200) scales burst up to backlog//4 at minimum
    (before jitter), so the helper can't fall arbitrarily far behind."""
    backlog = 1200  # backlog//4 = 300
    base = 12
    # Many samples; the minimum should never drop below
    # floor(300 * LOW) = 210 because backlog scaling already pushed the
    # base up to 300.
    samples = [_compute_reveal_burst(backlog=backlog, base=base) for _ in range(500)]
    expected_floor = max(1, int((backlog // 4) * _REVEAL_JITTER_LOW))
    assert min(samples) >= expected_floor


def test_full_buffer_drains_in_bounded_frames() -> None:
    """Stream a 5000-char buffer and confirm the cursor reaches the end
    in at most a generous frame budget. Belt-and-suspenders against
    jitter ever introducing a slow-down regression."""
    full_len = 5000
    base = 12
    pos = 0
    frames = 0
    max_frames = (
        1000  # at base=12 unjittered we'd finish in ~417; jitter+scaling pushes lower
    )
    while pos < full_len and frames < max_frames:
        burst = _compute_reveal_burst(backlog=full_len - pos, base=base)
        pos = min(full_len, pos + burst)
        frames += 1
    assert pos == full_len, f"cursor stalled at {pos}/{full_len} after {frames} frames"


def test_jitter_actually_jitters() -> None:
    """Two consecutive calls with the same input shouldn't both return
    the exact same value (probabilistically — over 50 samples we expect
    multiple distinct values). Guards against a regression where the
    jitter is accidentally seeded or constant."""
    samples = {_compute_reveal_burst(backlog=100, base=20) for _ in range(50)}
    assert len(samples) >= 5, f"jitter looks frozen — only {len(samples)} unique values"
