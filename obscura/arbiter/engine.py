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
    check_drift,
    check_file_quality,
    check_file_relevance,
    check_goal_transition,
    check_model_turn,
    check_retry_spiral,
    check_scope_creep,
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
        self._session_errors: dict[str, list[str]] = {}  # cross-turn error memory
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
        # 0. Inject historical errors for cross-turn spiral detection.
        target_id_early = self._extract_target_id(kind, context)
        if kind == ArbiterCheckKind.MODEL_TURN:
            historical = self._session_errors.get(target_id_early, [])
            if historical:
                existing_recent = context.get("recent_errors") or []
                context["recent_errors"] = historical + list(existing_recent)

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

        # 6. Challenge: before killing, ask the agent to justify continuing.
        # On first DENY for a target, downgrade to REVISE with a pointed
        # question. If the next evaluation still fails → real DENY/KILL.
        # Skip challenge for: safety violations, daemons, phantom 4-5 (autonomous).
        target_id = self._extract_target_id(kind, context)
        retry_count = self._retry_counts.get(target_id, 0)
        phantom = self._resolve_phantom_level()
        is_autonomous = phantom >= 4 or self._config.is_daemon

        if verdict in (ArbiterVerdict.DENY, ArbiterVerdict.KILL):
            has_safety = any(i.startswith("SAFETY:") for i in issues)
            if not has_safety and not is_autonomous and retry_count == 0:
                # First offense, interactive agent: challenge instead of killing.
                verdict = ArbiterVerdict.REVISE
                feedback = (
                    "CHALLENGE: Arbiter is about to stop you. "
                    "Briefly justify why this work is necessary and on-track, "
                    "or acknowledge the issue and change approach. "
                    f"Original issues: {'; '.join(issues)}"
                )

        # 7. Retry tracking
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

        # Record errors for cross-turn memory.
        if issues:
            error_list = self._session_errors.setdefault(target_id, [])
            error_list.extend(issues)
            # Cap history at 20 per target.
            if len(error_list) > 20:
                self._session_errors[target_id] = error_list[-20:]

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
            return self._check_model_turn(context)

        if kind == ArbiterCheckKind.TASK_COMPLETE:
            task = context.get("task") or {}
            return check_task_complete(
                task,
                output_text=str(context.get("output_text", "")),
            )

        if kind == ArbiterCheckKind.GOAL_TRANSITION:
            goal = context.get("goal") or {}
            return check_goal_transition(
                goal,
                linked_task_statuses=context.get("linked_task_statuses"),
            )

        return 1.0, []

    # ------------------------------------------------------------------
    # Combined model-turn check
    # ------------------------------------------------------------------

    def _check_model_turn(self, context: dict[str, Any]) -> tuple[float, list[str]]:
        """Run all model-turn checks and combine scores.

        Runs the base turn check (empty output, lint, spinning) plus
        optional scope-creep, drift, and retry-spiral checks when the
        context provides the necessary data.
        """
        all_issues: list[str] = []

        # 1. Base turn check (always).
        base_score, base_issues = check_model_turn(
            output_text=str(context.get("output_text", "")),
            tool_error_count=int(context.get("tool_error_count", 0)),
            repeated_errors=int(context.get("repeated_errors", 0)),
            lint_errors=context.get("lint_errors"),
        )
        all_issues.extend(base_issues)
        scores = [base_score]

        # 2. Scope creep (if task context available).
        task_subject = str(context.get("task_subject", ""))
        task_description = str(context.get("task_description", ""))
        if task_subject:
            scope_score, scope_issues = check_scope_creep(
                task_subject=task_subject,
                task_description=task_description,
                tool_call_count=int(context.get("tool_call_count", 0)),
                files_touched=context.get("files_touched") or [],
                turn_count=int(context.get("turn_count", 0)),
            )
            scores.append(scope_score)
            all_issues.extend(scope_issues)

        # 3. Drift (if task context + recent activity available).
        recent_tool_calls = context.get("recent_tool_calls") or []
        if task_subject and recent_tool_calls:
            drift_score, drift_issues = check_drift(
                task_subject=task_subject,
                task_description=task_description,
                recent_tool_calls=recent_tool_calls,
                recent_output=str(context.get("output_text", "")),
            )
            scores.append(drift_score)
            all_issues.extend(drift_issues)

        # 4. Retry spiral (if recent errors available).
        recent_errors = context.get("recent_errors") or []
        if len(recent_errors) >= 3:
            spiral_score, spiral_issues = check_retry_spiral(recent_errors)
            scores.append(spiral_score)
            all_issues.extend(spiral_issues)

        # 5. File quality (if files_touched available).
        files_touched = context.get("files_touched") or []
        if files_touched:
            fq_score, fq_issues = check_file_quality(files_touched)
            scores.append(fq_score)
            all_issues.extend(fq_issues)

        # 6. File relevance (if task context + files available).
        if task_subject and files_touched:
            fr_score, fr_issues = check_file_relevance(
                task_subject, task_description, files_touched
            )
            scores.append(fr_score)
            all_issues.extend(fr_issues)

        # Composite: take the minimum — one bad signal is enough to flag.
        final_score = min(scores) if scores else 1.0
        return max(final_score, 0.0), all_issues

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
        """Map composite score + issues to a verdict.

        Phantom level adjusts non-safety verdicts:
        - Level 0 (off): normal behavior
        - Level 1-3 (shadow/copilot/partner): DENY/KILL downgraded to
          REVISE — steer the agent back on track, give it a chance.
        - Level 4-5 (lead/takeover): DENY upgraded to KILL — at high
          autonomy the agent should know better. If it's wasting
          resources, cut it off fast instead of burning more tokens
          trying to steer.
        Safety violations (``SAFETY:`` prefix) always KILL regardless
        of phantom level.
        """
        # Safety violations → immediate KILL at any phantom level.
        has_safety = any(issue.startswith("SAFETY:") for issue in issues)
        if self._config.kill_on_safety_violation and has_safety:
            return ArbiterVerdict.KILL

        # Compute raw verdict from score thresholds.
        if composite >= self._config.accept_threshold:
            raw = ArbiterVerdict.ACCEPT
        elif composite >= self._config.revise_threshold:
            raw = ArbiterVerdict.REVISE
        else:
            raw = ArbiterVerdict.DENY

        # Daemons and high-autonomy agents (phantom 4-5): escalate DENY → KILL.
        # They run unsupervised — if they're wasting resources, kill fast.
        phantom = self._resolve_phantom_level()
        is_autonomous = phantom >= 4 or self._config.is_daemon

        if is_autonomous and raw == ArbiterVerdict.DENY:
            return ArbiterVerdict.KILL

        # Phantom 1-3 (interactive, lower autonomy): steer, don't stop.
        # Downgrade hard blocks to REVISE — give the agent a chance.
        if 1 <= phantom <= 3 and raw in (ArbiterVerdict.DENY, ArbiterVerdict.KILL):
            return ArbiterVerdict.REVISE

        return raw

    def _resolve_phantom_level(self) -> int:
        """Get the current phantom level (config override > env var > 0)."""
        if self._config.phantom_level > 0:
            return self._config.phantom_level
        import os

        try:
            return int(os.environ.get("OBSCURA_PHANTOM_LEVEL", "0"))
        except (ValueError, TypeError):
            return 0

    # ------------------------------------------------------------------
    # Feedback generation
    # ------------------------------------------------------------------

    def _generate_feedback(
        self,
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

        phantom = self._resolve_phantom_level()

        is_autonomous = phantom >= 4 or self._config.is_daemon

        if verdict == ArbiterVerdict.KILL:
            if is_autonomous:
                parts.insert(0, "KILLED: Wasting resources. Task aborted.")
            else:
                parts.insert(0, "CRITICAL: Action aborted by Arbiter.")
        elif verdict == ArbiterVerdict.DENY:
            parts.insert(0, "DENIED: Action blocked by Arbiter.")
        elif verdict == ArbiterVerdict.REVISE:
            if 1 <= phantom <= 3:
                parts.insert(
                    0,
                    "STEER: You're off-track. Correct course:",
                )
            else:
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
        """Best-effort persistence to the verdict store and daily log."""
        try:
            from obscura.arbiter.store import ArbiterStore

            ArbiterStore().record(event)
        except Exception:
            logger.debug("Could not persist arbiter event", exc_info=True)

        # Write non-ACCEPT verdicts to the KAIROS daily log.
        if event.verdict != ArbiterVerdict.ACCEPT:
            self._log_to_daily(event)

    @staticmethod
    def _log_to_daily(event: ArbiterEvent) -> None:
        """Append an Arbiter verdict to the KAIROS daily log."""
        try:
            from obscura.kairos.daily_log import DailyLog

            feedback_short = event.score.feedback[:80] if event.score.feedback else ""
            entry = (
                f"arbiter {event.verdict.value}: {event.kind.value} "
                f"[{event.target_id}] score={event.score.composite:.2f}"
            )
            if feedback_short:
                entry += f" — {feedback_short}"
            DailyLog().append(entry, source="arbiter")
        except Exception:
            pass

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
