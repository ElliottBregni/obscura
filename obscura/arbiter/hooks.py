"""obscura.arbiter.hooks — Supervisor hook registration for the Arbiter.

Registers Arbiter evaluation functions at the appropriate supervisor
hook points.  Follows the same pattern as
``obscura.core.supervisor.eval_hooks``.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, cast

from obscura.arbiter.checks import check_test_results
from obscura.arbiter.engine import ArbiterEngine
from obscura.arbiter.test_runner import run_related_tests
from obscura.arbiter.types import (
    ArbiterCheckKind,
    ArbiterConfig,
    ArbiterVerdict,
)
from obscura.core.supervisor.types import SupervisorHookPoint
from obscura.tools.system.file_state import get_recently_modified_files

logger = logging.getLogger(__name__)

# Module-level engine instance, shared across hooks in a session.
_engine: ArbiterEngine | None = None
_agent_loop: Any = None  # AgentLoop reference for mechanical kill.


def get_engine() -> ArbiterEngine | None:
    return _engine


def register_agent_loop(loop: Any) -> None:
    """Attach the active AgentLoop so the Arbiter can kill it mechanically."""
    global _agent_loop  # noqa: PLW0603
    _agent_loop = loop


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------


async def _pre_tool_handler(context: dict[str, Any]) -> dict[str, Any] | bool:
    """PRE_TOOL_EXECUTION: vet tool calls before they run."""
    engine = _engine
    if engine is None or not engine.is_running:
        return context

    score = await engine.evaluate(
        ArbiterCheckKind.TOOL_CALL,
        {
            "tool_name": context.get("tool_name", ""),
            "args": context.get("tool_input") or context.get("args") or {},
            "allowlist": context.get("tool_allowlist"),
            "denylist": context.get("tool_denylist"),
        },
    )

    if score.verdict == ArbiterVerdict.KILL:
        context["arbiter_killed"] = True
        context["arbiter_feedback"] = score.feedback
        _kill_loop(score.feedback)
        return False  # Block the tool call.
    if score.verdict == ArbiterVerdict.DENY:
        context["arbiter_denied"] = True
        context["arbiter_feedback"] = score.feedback
        return False
    if score.verdict == ArbiterVerdict.REVISE:
        context["arbiter_feedback"] = score.feedback
    return context


async def _post_turn_handler(context: dict[str, Any]) -> dict[str, Any]:
    """POST_MODEL_TURN: score agent output."""
    engine = _engine
    if engine is None or not engine.is_running:
        return context

    # Enrich context with recently modified files for quality + relevance checks.
    files_touched: list[str] = []
    try:
        files_touched = get_recently_modified_files(limit=20)
    except Exception:
        pass

    score = await engine.evaluate(
        ArbiterCheckKind.MODEL_TURN,
        {
            "output_text": context.get("output_text", ""),
            "tool_error_count": context.get("tool_error_count", 0),
            "repeated_errors": context.get("repeated_errors", 0),
            "lint_errors": context.get("eval_errors"),
            "summary": context.get("turn_summary", ""),
            "files_touched": files_touched,
            "task_subject": context.get("task_subject", ""),
            "task_description": context.get("task_description", ""),
            "tool_call_count": context.get("tool_call_count", 0),
            "turn_count": context.get("turn_count", 0),
            "recent_tool_calls": context.get("recent_tool_calls") or [],
            "recent_errors": context.get("recent_errors") or [],
        },
    )

    context["arbiter_score"] = score.composite
    context["arbiter_verdict"] = score.verdict.value
    if score.verdict == ArbiterVerdict.KILL:
        _kill_loop(score.feedback)
    if score.feedback:
        context["arbiter_feedback"] = score.feedback
    return context


async def _task_complete_handler(context: dict[str, Any]) -> dict[str, Any] | bool:
    """POST_TASK_COMPLETE: gate task completion."""
    engine = _engine
    if engine is None or not engine.is_running:
        return context

    task = cast(Mapping[str, Any], context.get("task") or {})
    score = await engine.evaluate(
        ArbiterCheckKind.TASK_COMPLETE,
        {
            "task": task,
            "output_text": str(task.get("output", "")),
        },
    )

    context["arbiter_score"] = score.composite
    context["arbiter_verdict"] = score.verdict.value
    if score.verdict in (ArbiterVerdict.DENY, ArbiterVerdict.KILL):
        context["arbiter_feedback"] = score.feedback
        return False  # Block completion.

    # Run related tests on ACCEPT — downgrade if failures found.
    if score.verdict == ArbiterVerdict.ACCEPT:
        test_feedback = await _run_tests_on_complete()
        if test_feedback:
            context["arbiter_verdict"] = "revise"
            context["arbiter_feedback"] = test_feedback
            return context

    if score.feedback:
        context["arbiter_feedback"] = score.feedback
    return context


async def _run_tests_on_complete() -> str:
    """Run related tests on task completion. Returns feedback string if failures."""
    try:
        files = get_recently_modified_files(limit=20)
        if not files:
            return ""

        outcome = await run_related_tests(files, timeout_s=10.0)
        if outcome.failed == 0 and outcome.errors == 0:
            return ""

        from dataclasses import asdict

        _test_score, test_issues = check_test_results(asdict(outcome))
        if test_issues:
            return f"Test failures after completion: {'; '.join(test_issues)}"
        return ""
    except Exception:
        return ""


async def _goal_transition_handler(context: dict[str, Any]) -> dict[str, Any] | bool:
    """POST_GOAL_TRANSITION: gate goal status changes."""
    engine = _engine
    if engine is None or not engine.is_running:
        return context

    score = await engine.evaluate(
        ArbiterCheckKind.GOAL_TRANSITION,
        {
            "goal": context.get("goal") or {},
            "linked_task_statuses": context.get("linked_task_statuses"),
        },
    )

    context["arbiter_score"] = score.composite
    context["arbiter_verdict"] = score.verdict.value
    if score.verdict in (ArbiterVerdict.DENY, ArbiterVerdict.KILL):
        context["arbiter_feedback"] = score.feedback
        return False
    if score.feedback:
        context["arbiter_feedback"] = score.feedback
    return context


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_arbiter_hooks(
    hooks: Any,
    *,
    config: ArbiterConfig | None = None,
    session_id: str = "",
    run_id: str = "",
) -> ArbiterEngine:
    """Register Arbiter hooks with a SessionHookManager.

    Creates and starts the engine, then registers handlers at the
    appropriate hook points.  Returns the engine instance.
    """
    global _engine  # noqa: PLW0603

    engine = ArbiterEngine(config=config, session_id=session_id, run_id=run_id)
    engine.start()
    _engine = engine

    _register(
        hooks,
        SupervisorHookPoint.PRE_TOOL_EXECUTION,
        "before",
        "arbiter:pre_tool",
        _pre_tool_handler,
    )
    _register(
        hooks,
        SupervisorHookPoint.POST_MODEL_TURN,
        "after",
        "arbiter:post_turn",
        _post_turn_handler,
    )

    # New hook points (may not exist in older supervisor versions).
    try:
        _register(
            hooks,
            SupervisorHookPoint.POST_TASK_COMPLETE,
            "after",
            "arbiter:task_complete",
            _task_complete_handler,
        )
    except (AttributeError, ValueError):
        logger.debug("POST_TASK_COMPLETE hook point not available")

    try:
        _register(
            hooks,
            SupervisorHookPoint.POST_GOAL_TRANSITION,
            "after",
            "arbiter:goal_transition",
            _goal_transition_handler,
        )
    except (AttributeError, ValueError):
        logger.debug("POST_GOAL_TRANSITION hook point not available")

    logger.info("Arbiter hooks registered (session=%s)", session_id)
    return engine


def _register(
    hooks: Any,
    point: Any,
    phase: str,
    ref: str,
    handler: Any,
) -> None:
    """Best-effort hook registration supporting multiple hook manager APIs."""
    if hasattr(hooks, "register"):
        hooks.register(point, phase, ref, handler, persist=False)
    elif hasattr(hooks, "bind_handler"):
        hooks.bind_handler(point.value, handler)
    elif hasattr(hooks, "add"):
        hooks.add(point, handler)
    else:
        logger.warning("Unknown hook manager API; could not register %s", ref)


def _kill_loop(reason: str) -> None:
    """Mechanically kill the agent loop. Not prompt injection — the loop stops."""
    loop = _agent_loop
    if loop is None:
        return
    try:
        kill_fn = getattr(loop, "arbiter_kill", None)
        if callable(kill_fn):
            kill_fn(reason)
            logger.info("Arbiter killed agent loop: %s", reason[:100])
    except Exception:
        logger.debug("Failed to kill agent loop", exc_info=True)
