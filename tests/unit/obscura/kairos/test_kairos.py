"""Tests for obscura.kairos — KAIROS daemon features."""

from __future__ import annotations

from typing import TYPE_CHECKING

from obscura.kairos.away_summary import generate_away_summary
from obscura.kairos.daily_log import DailyLog
from obscura.kairos.frustration import FrustrationDetector
from obscura.kairos.undercover import UndercoverMode

if TYPE_CHECKING:
    from pathlib import Path


def test_daily_log_append_and_read(tmp_path: Path, monkeypatch: object) -> None:
    # Monkey-patch log dir to tmp_path.
    import obscura.kairos.daily_log as dl

    monkeypatch.setattr(dl, "_log_dir", lambda: tmp_path)  # type: ignore[attr-defined]

    log = DailyLog()
    log._path = tmp_path / "test.md"
    log.append("Test entry 1", source="test")
    log.append("Test entry 2", source="test")
    content = log.read()
    assert "Test entry 1" in content
    assert "Test entry 2" in content
    assert log.entry_count() == 2


def test_frustration_detection() -> None:
    d = FrustrationDetector()
    assert d.analyze("wtf is going on").is_frustrated
    assert d.analyze("this sucks").is_frustrated
    assert not d.analyze("thanks, that works").is_frustrated
    assert d.analyze("thanks, that works").sentiment == "positive"
    assert not d.analyze("normal message here").is_frustrated
    assert d.analyze("keep going").sentiment == "continue"


def test_frustration_consecutive_tracking() -> None:
    d = FrustrationDetector()
    d.analyze("wtf")
    d.analyze("this is shit")
    result = d.analyze("ffs why is this broken")
    assert result.consecutive_frustrations == 3


def test_frustration_reset_on_positive() -> None:
    d = FrustrationDetector()
    d.analyze("wtf")
    d.analyze("thanks")  # positive resets streak
    result = d.analyze("damn it")
    assert result.consecutive_frustrations == 1


def test_undercover_sanitize() -> None:
    uc = UndercoverMode()
    uc.force(True)
    msg = "Fix auth\n\nCo-Authored-By: Claude AI <noreply@anthropic.com>"
    sanitized = uc.sanitize_commit_message(msg)
    assert "Claude" not in sanitized
    assert "Fix auth" in sanitized


def test_undercover_no_sanitize_when_off() -> None:
    uc = UndercoverMode()
    uc.force(False)
    msg = "Fix auth\n\nCo-Authored-By: Claude AI <noreply@anthropic.com>"
    assert uc.sanitize_commit_message(msg) == msg


async def test_away_summary() -> None:
    history = [
        ("user", "Fix the login bug in auth.py"),
        (
            "assistant",
            "I found the issue in auth.py line 42. The token expiry check was missing.",
        ),
    ]
    summary = await generate_away_summary(history)
    assert len(summary) > 0
    assert "Welcome back" in summary
