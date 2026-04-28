"""Tests for obscura.kairos — KAIROS daemon features."""

from __future__ import annotations

from typing import TYPE_CHECKING

from obscura.kairos.away_summary import generate_away_summary
from obscura.kairos.daily_log import DailyLog
from obscura.kairos.frustration import FrustrationDetector
from obscura.kairos.undercover import UndercoverMode

if TYPE_CHECKING:
    from collections.abc import Mapping
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


# ---------------------------------------------------------------------------
# Daemon-origin turn framing — guards against the model rationalising
# autonomous ticks as "the user sent an empty message".
# ---------------------------------------------------------------------------


class _RecordingLoop:
    """Stand-in AgentLoop that records every inject_user_input call."""

    def __init__(self) -> None:
        self.injected: list[str] = []

    def inject_user_input(self, text: str) -> None:
        self.injected.append(text)


def _make_engine_with_loop(
    monkeypatch: object,
    *,
    next_task: "Mapping[str, object] | None",
    active_goals: list[object] | None = None,
) -> tuple[object, _RecordingLoop]:
    """Build a KairosEngine wired to a recording loop, with TaskQueue and
    GoalBoard stubbed out so the test stays hermetic.
    """
    from obscura.kairos.engine import KairosEngine

    class _StubQueue:
        def reclaim_stale(self) -> None: ...
        def next_ready(self, **_: object) -> "Mapping[str, object] | None":
            return next_task

        def claim(self, *_: object, **__: object) -> bool:
            return next_task is not None

    class _StubBoard:
        def active_goals(self) -> list[object]:
            return active_goals or []

        def update(self, *_: object, **__: object) -> None: ...

    import obscura.core.task_queue as _tq
    import obscura.kairos.goals as _gb

    monkeypatch.setattr(_tq, "TaskQueue", _StubQueue)  # type: ignore[attr-defined]
    monkeypatch.setattr(_gb, "GoalBoard", _StubBoard)  # type: ignore[attr-defined]

    eng = KairosEngine.__new__(KairosEngine)
    loop = _RecordingLoop()
    eng._active_loop = loop  # type: ignore[attr-defined]
    return eng, loop


def test_proactive_tick_skipped_when_no_task_and_no_goal(
    monkeypatch: object,
) -> None:
    """No task claim and no goal hint → no inject. Avoids ghost empty turns."""
    eng, loop = _make_engine_with_loop(monkeypatch, next_task=None, active_goals=[])
    eng._on_proactive_tick(1)  # type: ignore[attr-defined]
    assert loop.injected == [], (
        "Empty fallback ticks must be dropped — they produce ghost user turns."
    )


def test_proactive_tick_claimed_task_wrapped_in_kairos_tag(
    monkeypatch: object,
) -> None:
    """Daemon-origin turns must be wrapped in <kairos> so the model can tell
    them apart from real user input."""
    task = {
        "task_id": "t-42",
        "priority": 5,
        "subject": "Refactor login",
        "description": "Split auth.py module",
        "goal_id": None,
    }
    eng, loop = _make_engine_with_loop(monkeypatch, next_task=task)
    eng._on_proactive_tick(7)  # type: ignore[attr-defined]
    assert len(loop.injected) == 1
    payload = loop.injected[0]
    assert payload.startswith("<kairos>"), payload
    assert payload.rstrip().endswith("</kairos>"), payload
    assert "<task " in payload and 't-42' in payload


def test_inject_user_input_drops_empty_payload() -> None:
    """AgentLoop.inject_user_input ignores whitespace-only / empty text.

    Belt-and-suspenders for the kairos fix: any other autonomous caller
    (supervisor probe, eval harness) that accidentally pushes empty text
    must not produce a ghost "user sent an empty message" turn either.
    """
    from obscura.core.agent_loop import AgentLoop
    from obscura.core.tools import ToolRegistry

    loop = AgentLoop(None, ToolRegistry())
    loop.inject_user_input("")
    loop.inject_user_input("   \n\t")
    assert loop._user_input_queue.empty()  # type: ignore[attr-defined]

    loop.inject_user_input("real payload")
    assert loop._user_input_queue.qsize() == 1  # type: ignore[attr-defined]
