"""Unit tests for obscura.arbiter.engine — verdict logic and scoring."""

from __future__ import annotations

import pytest

from obscura.arbiter.engine import ArbiterEngine
from obscura.arbiter.types import (
    ArbiterCheckKind,
    ArbiterConfig,
    ArbiterVerdict,
)


@pytest.fixture()
def engine() -> ArbiterEngine:
    """Return an ArbiterEngine with judge disabled for deterministic tests."""
    config = ArbiterConfig(
        enabled=True,
        judge_mode="never",
        accept_threshold=0.8,
        revise_threshold=0.3,
        max_retries=2,
    )
    eng = ArbiterEngine(config=config, session_id="test", run_id="run-1")
    eng.start()
    return eng


# ---------------------------------------------------------------------------
# Verdict mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_tool_call_accepted(engine: ArbiterEngine) -> None:
    score = await engine.evaluate(
        ArbiterCheckKind.TOOL_CALL,
        {"tool_name": "read_text_file", "args": {"path": "/tmp/x.py"}},
    )
    assert score.verdict == ArbiterVerdict.ACCEPT
    assert score.composite >= 0.8


@pytest.mark.asyncio
async def test_dangerous_tool_call_killed(engine: ArbiterEngine) -> None:
    score = await engine.evaluate(
        ArbiterCheckKind.TOOL_CALL,
        {"tool_name": "run_shell", "args": {"command": "rm -rf /"}},
    )
    assert score.verdict == ArbiterVerdict.KILL
    assert "SAFETY" in score.feedback


@pytest.mark.asyncio
async def test_denylisted_tool_killed(engine: ArbiterEngine) -> None:
    score = await engine.evaluate(
        ArbiterCheckKind.TOOL_CALL,
        {"tool_name": "evil", "args": {"x": 1}, "denylist": ["evil"]},
    )
    assert score.verdict == ArbiterVerdict.KILL


@pytest.mark.asyncio
async def test_empty_model_turn_revised(engine: ArbiterEngine) -> None:
    score = await engine.evaluate(
        ArbiterCheckKind.MODEL_TURN,
        {"output_text": "", "tool_error_count": 2},
    )
    # Empty output (-0.3) + 2 errors (-0.4) = 0.3 → REVISE or DENY
    assert score.verdict in (ArbiterVerdict.REVISE, ArbiterVerdict.DENY)
    assert score.feedback != ""


@pytest.mark.asyncio
async def test_clean_task_complete_accepted(engine: ArbiterEngine) -> None:
    score = await engine.evaluate(
        ArbiterCheckKind.TASK_COMPLETE,
        {"task": {"output": "all tests passed", "error": "", "retry_count": 0, "max_retries": 3}},
    )
    assert score.verdict == ArbiterVerdict.ACCEPT


@pytest.mark.asyncio
async def test_task_complete_no_output_revised(engine: ArbiterEngine) -> None:
    score = await engine.evaluate(
        ArbiterCheckKind.TASK_COMPLETE,
        {"task": {"output": "", "error": "", "retry_count": 0}},
    )
    assert score.verdict in (ArbiterVerdict.REVISE, ArbiterVerdict.ACCEPT)
    # Score should be penalized even if verdict is accept.
    assert score.deterministic < 1.0


@pytest.mark.asyncio
async def test_goal_complete_with_pending_tasks_penalized(engine: ArbiterEngine) -> None:
    score = await engine.evaluate(
        ArbiterCheckKind.GOAL_TRANSITION,
        {
            "goal": {"status": "completed", "progress": 100},
            "linked_task_statuses": ["completed", "pending", "pending"],
        },
    )
    assert score.composite < 0.8


# ---------------------------------------------------------------------------
# Retry escalation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_escalation_to_deny(engine: ArbiterEngine) -> None:
    """After max_retries REVISE verdicts, escalate to DENY."""
    for _ in range(3):  # max_retries=2, so 3rd should escalate
        score = await engine.evaluate(
            ArbiterCheckKind.MODEL_TURN,
            {"output_text": "", "tool_error_count": 1},
        )
    assert score.verdict == ArbiterVerdict.DENY
    assert "Max retries" in score.feedback


@pytest.mark.asyncio
async def test_accept_clears_retry_counter(engine: ArbiterEngine) -> None:
    """Successful evaluation should clear the retry counter."""
    # Trigger a revise.
    await engine.evaluate(
        ArbiterCheckKind.MODEL_TURN,
        {"output_text": "", "tool_error_count": 1},
    )
    assert len(engine._retry_counts) > 0

    # Now succeed.
    score = await engine.evaluate(
        ArbiterCheckKind.MODEL_TURN,
        {"output_text": "I fixed the bug and all tests pass."},
    )
    assert score.verdict == ArbiterVerdict.ACCEPT


# ---------------------------------------------------------------------------
# Event recording
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_recorded(engine: ArbiterEngine) -> None:
    await engine.evaluate(
        ArbiterCheckKind.TOOL_CALL,
        {"tool_name": "read_text_file", "args": {"path": "/tmp/x"}},
    )
    assert len(engine.events) == 1
    assert engine.events[0].kind == ArbiterCheckKind.TOOL_CALL


# ---------------------------------------------------------------------------
# Status / introspection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status(engine: ArbiterEngine) -> None:
    await engine.evaluate(
        ArbiterCheckKind.TOOL_CALL,
        {"tool_name": "read_text_file", "args": {"path": "/tmp/x"}},
    )
    status = engine.status()
    assert status["running"] is True
    assert status["evaluations"] == 1
    assert "accept" in status["verdict_counts"]


# ---------------------------------------------------------------------------
# Combined model turn checks (scope creep, drift, spiral)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_creep_flagged_on_model_turn(engine: ArbiterEngine) -> None:
    """A small task with excessive tool calls should get REVISE or DENY."""
    score = await engine.evaluate(
        ArbiterCheckKind.MODEL_TURN,
        {
            "output_text": "I refactored everything.",
            "task_subject": "Fix typo",
            "task_description": "Change 'teh' to 'the' in README",
            "tool_call_count": 50,
            "files_touched": [f"f{i}.py" for i in range(15)],
            "turn_count": 10,
        },
    )
    assert score.composite < 0.8
    assert any("scope" in d.lower() or "excessive" in d.lower() for d in score.details)


@pytest.mark.asyncio
async def test_retry_spiral_flagged_on_model_turn(engine: ArbiterEngine) -> None:
    """Repeated similar errors should get flagged."""
    score = await engine.evaluate(
        ArbiterCheckKind.MODEL_TURN,
        {
            "output_text": "Trying again...",
            "recent_errors": [
                "ConnectionRefusedError: port 5432",
                "ConnectionRefusedError: port 5432",
                "ConnectionRefusedError: port 5432",
                "ConnectionRefusedError: port 5432",
            ],
        },
    )
    assert score.composite < 0.8
    assert any("spiral" in d.lower() or "stuck" in d.lower() for d in score.details)


# ---------------------------------------------------------------------------
# Phantom level verdict adjustments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phantom_low_downgrades_deny_to_revise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phantom 1-3: non-safety DENY → REVISE (steer, don't stop)."""
    monkeypatch.setenv("OBSCURA_PHANTOM_LEVEL", "2")
    config = ArbiterConfig(judge_mode="never", accept_threshold=0.8, revise_threshold=0.3)
    eng = ArbiterEngine(config=config, session_id="test")
    eng.start()

    # This would normally DENY (empty output + errors = low score).
    score = await eng.evaluate(
        ArbiterCheckKind.MODEL_TURN,
        {"output_text": "", "tool_error_count": 5},
    )
    assert score.verdict == ArbiterVerdict.REVISE
    assert "STEER" in score.feedback


@pytest.mark.asyncio
async def test_phantom_high_escalates_deny_to_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phantom 4-5: non-safety DENY → KILL (don't waste tokens steering)."""
    monkeypatch.setenv("OBSCURA_PHANTOM_LEVEL", "5")
    config = ArbiterConfig(judge_mode="never", accept_threshold=0.8, revise_threshold=0.3)
    eng = ArbiterEngine(config=config, session_id="test")
    eng.start()

    score = await eng.evaluate(
        ArbiterCheckKind.MODEL_TURN,
        {"output_text": "", "tool_error_count": 5},
    )
    assert score.verdict == ArbiterVerdict.KILL
    assert "KILLED" in score.feedback or "Wasting" in score.feedback


@pytest.mark.asyncio
async def test_daemon_escalates_deny_to_kill() -> None:
    """Daemon agents: DENY → KILL."""
    config = ArbiterConfig(judge_mode="never", is_daemon=True)
    eng = ArbiterEngine(config=config, session_id="test")
    eng.start()

    score = await eng.evaluate(
        ArbiterCheckKind.MODEL_TURN,
        {"output_text": "", "tool_error_count": 5},
    )
    assert score.verdict == ArbiterVerdict.KILL


@pytest.mark.asyncio
async def test_safety_always_kills_regardless_of_phantom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Safety violations KILL even at phantom 1 (steer mode)."""
    monkeypatch.setenv("OBSCURA_PHANTOM_LEVEL", "1")
    config = ArbiterConfig(judge_mode="never")
    eng = ArbiterEngine(config=config, session_id="test")
    eng.start()

    score = await eng.evaluate(
        ArbiterCheckKind.TOOL_CALL,
        {"tool_name": "run_shell", "args": {"command": "rm -rf /"}},
    )
    assert score.verdict == ArbiterVerdict.KILL


@pytest.mark.asyncio
async def test_phantom_zero_normal_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phantom 0 (off): normal DENY, no adjustment."""
    monkeypatch.setenv("OBSCURA_PHANTOM_LEVEL", "0")
    config = ArbiterConfig(judge_mode="never", accept_threshold=0.8, revise_threshold=0.3)
    eng = ArbiterEngine(config=config, session_id="test")
    eng.start()

    score = await eng.evaluate(
        ArbiterCheckKind.MODEL_TURN,
        {"output_text": "", "tool_error_count": 5},
    )
    assert score.verdict == ArbiterVerdict.DENY
