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
