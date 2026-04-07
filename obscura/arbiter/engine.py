"""obscura.arbiter.engine — The Arbiter: system-level agent judge.

Like KAIROS is the daemon engine, Arbiter is the quality engine. It
watches agent actions via supervisor hooks and issues verdicts:
ACCEPT, REVISE, DENY, or KILL.

Scoring pipeline:
  1. Fast deterministic checks (always)
  2. LLM-as-judge (on ambiguity or always, budget-gated)
  3. Composite score → verdict mapping
  4. Feedback injection on REVISE (agent retries)

Usage::

    engine = ArbiterEngine()
    score = await engine.evaluate(
        ArbiterCheckKind.TASK_COMPLETE,
        {"task": task_dict, "output_text": "..."},
    )
    if score.verdict == ArbiterVerdict.DENY:
        # Block the action
        ...
"""

from __future__ import annotations

import logging
import time
from typing import Any

from obscura.arbiter.checks import (
    check_goal_transition,
    check_model_turn,
    check_task_complete,
    check_tool_call,
)
from obscura.arbiter.types import (
    ArbiterCheckKind,
    ArbiterConfig,
    ArbiterEvent,
    ArbiterScore,
    ArbiterVerdict,
)

logger = logging.getLogger(__name__)


class ArbiterEngine:
    """System-level judge daemon for agent quality gating.

    Instantiate once per session. Thread-safe for reads; writes
    (judge call counter, retry tracker) are session-scoped and
    single-threaded within a supervisor run.
    """

    def __init__(
        self,
        config: ArbiterConfig | None = None,
        *,
        session_id: str = "",
        run_id: str = "",
    ) -> None:
        self._config = config or ArbiterConfig()
        self._session_id = session_id
        self._run_id = run_id
        self._judge_calls = 0
        self._retry_counts: dict[str, int] = {}
        self._events: list[ArbiterEvent] = []
        self._started = False

    @property
    def config(self) -> ArbiterConfig:
        return self._config

    @property
    def is_running(self) -> bool:
        return self._started

    @property
    def events(self) -> list[ArbiterEvent]:
        return list(self._events)

    def start(self) -> None:
        self._started = True
        logger.info(
            "Arbiter started (judge_mode=%s, accept=%.1f, revise=%.1f)",
            self._config.judge_mode,
            self._config.accept_threshold,
            self._config.revise_threshold,
        )

    def stop(self) -> None:
        self._started = False
        logger.info(
            "Arbiter stopped — %d evaluations, %d judge calls",
            len(self._events),
            self._judge_calls,
        )

    # ------------------------------------------------------------------
    # Main evaluation entry point
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        kind: ArbiterCheckKind,
        context: dict[str, Any],
    ) -> ArbiterScore:
        """Run the full scoring pipeline and return a verdict.

        *context* keys depend on *kind*:

        - TOOL_CALL: tool_name, args, allowlist?, denylist?
        - MODEL_TURN: output_text, tool_error_count?, repeated_errors?, lint_errors?
        - TASK_COMPLETE: task (dict from TaskQueue.get)
        - GOAL_TRANSITION: goal (dict), linked_task_statuses?
        """
        # 1. Deterministic checks
        det_score, issues = self._run_checks(kind, context)

        # 2. Optional LLM judge
        judge_score: float | None = None
        judge_reasoning = ""
        if self._config.enabled:
            judge_score, judge_reasoning = await self._maybe_judge(
                det_score, kind, context, issues
            )

        # 3. Composite score
        composite = self._compute_composite(det_score, judge_score)

        # 4. Verdict
        verdict = self._verdict_from_score(composite, issues, kind, context)

        # 5. Feedback generation
        feedback = self._generate_feedback(verdict, issues, judge_reasoning)

        # 6. Retry tracking
        target_id = self._extract_target_id(kind, context)
        retry_count = self._retry_counts.get(target_id, 0)
        if verdict == ArbiterVerdict.REVISE:
            self._retry_counts[target_id] = retry_count + 1
            # Escalate to DENY after max retries.
            if self._retry_counts[target_id] > self._config.max_retries:
                verdict = ArbiterVerdict.DENY
                feedback = (
                    f"Max retries ({self._config.max_retries}) exceeded. {feedback}"
                )
        elif verdict == ArbiterVerdict.ACCEPT:
            # Clear retry counter on success.
            self._retry_counts.pop(target_id, None)

        score = ArbiterScore(
            deterministic=det_score,
            judge=judge_score,
            composite=composite,
            verdict=verdict,
            feedback=feedback,
            check_kind=kind,
            details=tuple(issues),
        )

        # Record event.
        event = ArbiterEvent(
            kind=kind,
            verdict=verdict,
            score=score,
            target_id=target_id,
            session_id=self._session_id,
            run_id=self._run_id,
            retry_count=retry_count,
        )
        self._events.append(event)
        self._persist_event(event)

        if verdict != ArbiterVerdict.ACCEPT:
            logger.info(
                "Arbiter %s: %s [%s] score=%.2f — %s",
                verdict.value,
                kind.value,
                target_id,
                composite,
                feedback[:100],
            )

        return score

    # ------------------------------------------------------------------
    # Deterministic checks
    # ------------------------------------------------------------------

    def _run_checks(
        self,
        kind: ArbiterCheckKind,
        context: dict[str, Any],
    ) -> tuple[float, list[str]]:
        """Dispatch to the appropriate deterministic checker."""
        if kind == ArbiterCheckKind.TOOL_CALL:
            return check_tool_call(
                tool_name=str(context.get("tool_name", "")),
                args=context.get("args") or {},
                allowlist=context.get("allowlist"),
                denylist=context.get("denylist"),
            )

        if kind == ArbiterCheckKind.MODEL_TURN:
            return check_model_turn(
                output_text=str(context.get("output_text", "")),
                tool_error_count=int(context.get("tool_error_count", 0)),
                repeated_errors=int(context.get("repeated_errors", 0)),
                lint_errors=context.get("lint_errors"),
            )

        if kind == ArbiterCheckKind.TASK_COMPLETE:
            task = context.get("task") or {}
            return check_task_complete(task)

        if kind == ArbiterCheckKind.GOAL_TRANSITION:
            goal = context.get("goal") or {}
            return check_goal_transition(
                goal,
                linked_task_statuses=context.get("linked_task_statuses"),
            )

        return 1.0, []

    # ------------------------------------------------------------------
    # LLM judge
    # ------------------------------------------------------------------

    async def _maybe_judge(
        self,
        det_score: float,
        kind: ArbiterCheckKind,
        context: dict[str, Any],
        issues: list[str],
    ) -> tuple[float | None, str]:
        """Conditionally invoke the LLM judge."""
        try:
            from obscura.arbiter.judge import maybe_judge

            score, reasoning = await maybe_judge(
                det_score,
                kind,
                context,
                self._config,
                session_judge_calls=self._judge_calls,
                issues=issues,
            )
            if score is not None:
                self._judge_calls += 1
            return score, reasoning
        except Exception:
            logger.debug("Judge invocation failed", exc_info=True)
            return None, ""

    # ------------------------------------------------------------------
    # Composite scoring
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_composite(deterministic: float, judge: float | None) -> float:
        """Weighted combination of deterministic and judge scores."""
        if judge is None:
            return deterministic
        # 60/40 weighting, same as eval/scoring.py
        return 0.6 * deterministic + 0.4 * judge

    # ------------------------------------------------------------------
    # Verdict mapping
    # ------------------------------------------------------------------

    def _verdict_from_score(
        self,
        composite: float,
        issues: list[str],
        kind: ArbiterCheckKind,
        context: dict[str, Any],
    ) -> ArbiterVerdict:
        """Map composite score + issues to a verdict."""
        # Safety violations → immediate KILL.
        if self._config.kill_on_safety_violation:
            if any(issue.startswith("SAFETY:") for issue in issues):
                return ArbiterVerdict.KILL

        if composite >= self._config.accept_threshold:
            return ArbiterVerdict.ACCEPT
        if composite >= self._config.revise_threshold:
            return ArbiterVerdict.REVISE
        return ArbiterVerdict.DENY

    # ------------------------------------------------------------------
    # Feedback generation
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_feedback(
        verdict: ArbiterVerdict,
        issues: list[str],
        judge_reasoning: str,
    ) -> str:
        """Build human-readable feedback for the agent."""
        if verdict == ArbiterVerdict.ACCEPT:
            return ""

        parts: list[str] = []
        if issues:
            parts.append("Issues: " + "; ".join(issues))
        if judge_reasoning:
            parts.append("Judge: " + judge_reasoning)
        if verdict == ArbiterVerdict.KILL:
            parts.insert(0, "CRITICAL: Action aborted by Arbiter.")
        elif verdict == ArbiterVerdict.DENY:
            parts.insert(0, "DENIED: Action blocked by Arbiter.")
        elif verdict == ArbiterVerdict.REVISE:
            parts.insert(0, "REVISE: Please fix the following and retry.")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_target_id(kind: ArbiterCheckKind, context: dict[str, Any]) -> str:
        """Extract a stable identifier for retry tracking."""
        if kind == ArbiterCheckKind.TOOL_CALL:
            return f"tool:{context.get('tool_name', 'unknown')}"
        if kind == ArbiterCheckKind.TASK_COMPLETE:
            task = context.get("task") or {}
            return f"task:{task.get('task_id', 'unknown')}"
        if kind == ArbiterCheckKind.GOAL_TRANSITION:
            goal = context.get("goal") or {}
            return f"goal:{goal.get('id', 'unknown')}"
        return f"turn:{int(time.time())}"

    def _persist_event(self, event: ArbiterEvent) -> None:
        """Best-effort persistence to the verdict store."""
        try:
            from obscura.arbiter.store import ArbiterStore

            ArbiterStore().record(event)
        except Exception:
            logger.debug("Could not persist arbiter event", exc_info=True)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return engine diagnostics."""
        verdicts = [e.verdict.value for e in self._events]
        return {
            "running": self._started,
            "config": {
                "judge_mode": self._config.judge_mode,
                "accept_threshold": self._config.accept_threshold,
                "revise_threshold": self._config.revise_threshold,
            },
            "evaluations": len(self._events),
            "judge_calls": self._judge_calls,
            "verdict_counts": {v: verdicts.count(v) for v in set(verdicts)},
            "active_retries": dict(self._retry_counts),
        }
