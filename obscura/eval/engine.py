"""Async execution engine for eval cases."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING

from obscura.core.agent_loop import AgentLoop
from obscura.core.hooks import HookRegistry
from obscura.core.types import AgentEventKind
from obscura.eval.models import (
    EvalCaseResult,
    EvalRunSummary,
    EvalVerdict,
    ToolCallRecord,
)
from obscura.eval.regression import compare_with_threshold
from obscura.eval.scoring import (
    compute_composite,
    score_deterministic,
    score_with_judge,
)

if TYPE_CHECKING:
    from obscura.core.event_store import EventStoreProtocol
    from obscura.core.tools import ToolRegistry
    from obscura.core.types import AgentEvent, BackendProtocol
    from obscura.eval.models import CompiledEvalCase
    from obscura.eval.store import EvalResultStore

logger = logging.getLogger(__name__)


class EvalEngine:
    """Async engine that executes compiled eval cases through AgentLoop."""

    def __init__(
        self,
        backend: BackendProtocol,
        tool_registry: ToolRegistry,
        *,
        event_store: EventStoreProtocol | None = None,
        judge_backend: BackendProtocol | None = None,
        result_store: EvalResultStore | None = None,
    ) -> None:
        self._backend = backend
        self._tool_registry = tool_registry
        self._event_store = event_store
        self._judge_backend = judge_backend
        self._result_store = result_store

    async def run_case(
        self,
        case: CompiledEvalCase,
        run_id: str,
    ) -> EvalCaseResult:
        """Execute a single eval case and return the scored result."""
        start_ms = time.monotonic_ns() // 1_000_000

        hooks = HookRegistry()
        session_id = f"eval-{run_id}-{case.id}"

        # Register tools with the backend so it can offer them to the model
        if self._tool_registry is not None:  # pyright: ignore[reportUnnecessaryComparison]
            try:
                for spec in self._tool_registry.all():
                    self._backend.register_tool(spec)
            except (AttributeError, TypeError):
                pass  # backend doesn't support register_tool

        # Start the backend (with timeout to protect against blocking backends)
        try:
            await asyncio.wait_for(
                self._backend.start(),
                timeout=10.0,
            )
        except (TimeoutError, Exception):
            pass  # already started, not required, or timed out

        loop = AgentLoop(
            backend=self._backend,
            tool_registry=self._tool_registry,
            max_turns=case.max_turns,
            hooks=hooks,
            event_store=self._event_store,
            agent_name=f"eval:{case.id}",
        )

        # Collect events
        collected_events: list[AgentEvent] = []
        output_text = ""
        tool_calls: list[ToolCallRecord] = []
        event_kinds: list[str] = []
        current_turn = 0

        try:
            async for event in loop.run(case.prompt, session_id=session_id):
                collected_events.append(event)
                event_kinds.append(event.kind.value)

                if event.kind == AgentEventKind.TEXT_DELTA:
                    output_text += event.text
                elif event.kind == AgentEventKind.TURN_START:
                    current_turn = event.turn
                elif event.kind == AgentEventKind.TOOL_CALL:
                    tool_calls.append(
                        ToolCallRecord(
                            turn=event.turn or current_turn,
                            tool_name=event.tool_name,
                            tool_input=dict(event.tool_input)
                            if event.tool_input
                            else {},
                        ),
                    )
                elif event.kind == AgentEventKind.TOOL_RESULT:
                    # Update the last matching tool call with its result
                    for tc in reversed(tool_calls):
                        if tc.tool_name and not tc.tool_result:
                            # Create new record with result (frozen, so rebuild)
                            idx = tool_calls.index(tc)
                            tool_calls[idx] = ToolCallRecord(
                                turn=tc.turn,
                                tool_name=tc.tool_name,
                                tool_input=tc.tool_input,
                                tool_result=event.tool_result or event.text,
                                is_error=event.is_error,
                                latency_ms=tc.latency_ms,
                            )
                            break

        except Exception as exc:
            elapsed_ms = int(time.monotonic_ns() // 1_000_000 - start_ms)
            logger.exception("Eval case %s failed: %s", case.id, exc)
            return EvalCaseResult(
                case_id=case.id,
                suite_id=case.suite_id,
                run_id=run_id,
                verdict=EvalVerdict.ERROR,
                deterministic_score=0.0,
                error=str(exc),
                latency_ms=elapsed_ms,
            )

        elapsed_ms = int(time.monotonic_ns() // 1_000_000 - start_ms)
        tool_calls_tuple = tuple(tool_calls)
        events_tuple = tuple(event_kinds)

        # Score deterministically
        det_score, assertion_outcomes = score_deterministic(
            case,
            events_tuple,
            output_text,
            tool_calls_tuple,
        )

        # Score with LLM judge if configured
        judge_score_val: float | None = None
        judge_detail = None
        if case.judge_criteria and self._judge_backend is not None:
            judge_detail = await score_with_judge(
                case,
                output_text,
                tool_calls_tuple,
                self._judge_backend,
            )
            judge_score_val = judge_detail.score

        composite = compute_composite(det_score, judge_score_val)

        # Determine verdict
        verdict = EvalVerdict.PASS if det_score >= 1.0 else EvalVerdict.FAIL
        if judge_score_val is not None and case.judge_criteria:
            if judge_score_val < case.judge_pass_threshold:
                verdict = EvalVerdict.FAIL
            elif det_score >= 1.0:
                verdict = EvalVerdict.PASS

        result = EvalCaseResult(
            case_id=case.id,
            suite_id=case.suite_id,
            run_id=run_id,
            verdict=verdict,
            deterministic_score=det_score,
            judge_score=judge_score_val,
            composite_score=composite,
            assertion_outcomes=assertion_outcomes,
            judge_detail=judge_detail,
            tool_calls_observed=tool_calls_tuple,
            output_text=output_text,
            turns_used=current_turn,
            latency_ms=elapsed_ms,
            events=events_tuple,
        )

        # Check regression if store is available
        if self._result_store is not None and (
            case.regression_baseline_run_id or case.regression_score_threshold < 1.0
        ):
            comparison = await compare_with_threshold(
                result,
                self._result_store,
                score_threshold=case.regression_score_threshold,
                max_score_delta=case.regression_max_score_delta,
            )
            if comparison is not None and comparison.is_regression:
                result = EvalCaseResult(
                    case_id=result.case_id,
                    suite_id=result.suite_id,
                    run_id=result.run_id,
                    verdict=EvalVerdict.REGRESSION,
                    deterministic_score=result.deterministic_score,
                    judge_score=result.judge_score,
                    composite_score=result.composite_score,
                    assertion_outcomes=result.assertion_outcomes,
                    judge_detail=result.judge_detail,
                    tool_calls_observed=result.tool_calls_observed,
                    output_text=result.output_text,
                    turns_used=result.turns_used,
                    latency_ms=result.latency_ms,
                    error=f"Regression: {comparison.details}",
                    events=result.events,
                )

        return result

    async def run_suite(
        self,
        cases: tuple[CompiledEvalCase, ...],
        suite_id: str,
    ) -> EvalRunSummary:
        """Execute all cases in a suite and produce an aggregated summary."""
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        results: list[EvalCaseResult] = []

        for case in cases:
            result = await self.run_case(case, run_id)
            results.append(result)
            logger.info(
                "Case %s: %s (det=%.2f, composite=%.2f)",
                case.id,
                result.verdict.value,
                result.deterministic_score,
                result.composite_score,
            )

        passed = sum(1 for r in results if r.verdict == EvalVerdict.PASS)
        failed = sum(1 for r in results if r.verdict == EvalVerdict.FAIL)
        regressions = sum(1 for r in results if r.verdict == EvalVerdict.REGRESSION)
        errors = sum(1 for r in results if r.verdict == EvalVerdict.ERROR)

        det_scores = [r.deterministic_score for r in results]
        judge_scores = [r.judge_score for r in results if r.judge_score is not None]
        composite_scores = [r.composite_score for r in results]

        avg_det = sum(det_scores) / len(det_scores) if det_scores else 0.0
        avg_judge = sum(judge_scores) / len(judge_scores) if judge_scores else None
        avg_composite = (
            sum(composite_scores) / len(composite_scores) if composite_scores else 0.0
        )

        summary = EvalRunSummary(
            run_id=run_id,
            suite_id=suite_id,
            backend=cases[0].backend if cases else "",
            model=cases[0].model if cases else "",
            total_cases=len(results),
            passed=passed,
            failed=failed,
            regressions=regressions,
            errors=errors,
            avg_deterministic_score=avg_det,
            avg_judge_score=avg_judge,
            avg_composite_score=avg_composite,
            case_results=tuple(results),
        )

        # Persist if store is available
        if self._result_store is not None:
            await self._result_store.save_run(summary)

        return summary
