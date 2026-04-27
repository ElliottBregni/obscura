"""Tests for obscura.auth.session_activity."""

from __future__ import annotations

import pytest

from obscura.auth.session_activity import IdleTimeoutTracker


def test_unknown_session_is_not_idle() -> None:
    """Unknown = never observed, which is NOT idle — idle means
    'was active, now isn't.'"""
    tracker = IdleTimeoutTracker(idle_max_seconds=60)
    assert tracker.is_idle("never-seen") is False


def test_observed_session_is_not_idle_immediately() -> None:
    tracker = IdleTimeoutTracker(idle_max_seconds=60)
    tracker.observe("s1", now=100.0)
    assert tracker.is_idle("s1", now=100.0) is False


def test_session_becomes_idle_past_window() -> None:
    tracker = IdleTimeoutTracker(idle_max_seconds=60)
    tracker.observe("s1", now=100.0)
    assert tracker.is_idle("s1", now=200.0) is True  # 100s of inactivity


def test_observing_resets_idle_clock() -> None:
    tracker = IdleTimeoutTracker(idle_max_seconds=60)
    tracker.observe("s1", now=100.0)
    tracker.observe("s1", now=150.0)
    # Now window is measured from 150, not 100.
    assert tracker.is_idle("s1", now=200.0) is False
    assert tracker.is_idle("s1", now=220.0) is True


def test_forget_removes_record() -> None:
    tracker = IdleTimeoutTracker(idle_max_seconds=60)
    tracker.observe("s1", now=100.0)
    tracker.forget("s1")
    # Now unknown, so not idle.
    assert tracker.is_idle("s1", now=100.0) is False
    assert tracker.size() == 0


def test_empty_session_id_is_noop() -> None:
    tracker = IdleTimeoutTracker(idle_max_seconds=60)
    tracker.observe("", now=100.0)
    assert tracker.size() == 0


def test_first_seen_preserved_across_observations() -> None:
    tracker = IdleTimeoutTracker(idle_max_seconds=60)
    tracker.observe("s1", now=100.0)
    tracker.observe("s1", now=150.0)
    tracker.observe("s1", now=200.0)
    # Internal: verify first_seen stays at 100.
    # (accessed via _activity for white-box test.)
    sample = tracker._activity["s1"]  # type: ignore[reportPrivateUsage]
    assert sample.first_seen == 100.0
    assert sample.last_seen == 200.0


def test_env_var_configures_idle_max(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSCURA_SESSION_IDLE_MAX", "42")
    tracker = IdleTimeoutTracker()
    assert tracker.idle_max_seconds == 42.0


def test_env_var_rejects_non_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSCURA_SESSION_IDLE_MAX", "not-a-number")
    tracker = IdleTimeoutTracker()
    assert tracker.idle_max_seconds == 60 * 60  # default


def test_env_var_rejects_zero_or_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OBSCURA_SESSION_IDLE_MAX", "0")
    tracker = IdleTimeoutTracker()
    assert tracker.idle_max_seconds == 60 * 60


def test_pruning_drops_stale_entries() -> None:
    tracker = IdleTimeoutTracker(idle_max_seconds=10)
    tracker.observe("s1", now=100.0)
    tracker.observe("s2", now=100.0)
    # Observe forces a prune check if enough time has elapsed.
    # _PRUNE_INTERVAL_SECONDS is 300, so we need to jump past that.
    tracker.observe("s3", now=500.0)
    # s1 and s2 last_seen=100; now=500; idle_max=10; stale by 390s. Pruned.
    # s3 is fresh.
    assert tracker.size() == 1
    assert tracker.is_idle("s3", now=500.0) is False
