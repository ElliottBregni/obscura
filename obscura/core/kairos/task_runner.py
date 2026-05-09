"""obscura.core.kairos.task_runner — Executes a single Task via the agent loop.

Drives one model turn (or a short loop) to complete a task.
Returns a TaskResult.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from obscura.core.enums.agent import AgentEventKind
from obscura.core.enums.lifecycle import KairosTaskStatus
from obscura.core.kairos.errors import (
    BudgetExceededError,
)
from obscura.core.kairos.types import (
    BudgetUsage,
    Goal,
    KairosConfig,
    Task,
    TaskResult,
)

if TYPE_CHECKING:
    from obscura.core.agent_loop_v2 import AgentLoopV2
    from obscura.core.kairos.goal_store import GoalStoreProtocol

logger = logging.getLogger(__name__)


# Event kinds whose .text payload should accumulate into the task's output.
# Only TEXT_DELTA — THINKING_DELTA is internal reasoning, not user-visible
# output, and TOOL_CALL/TOOL_RESULT carry structured data, not assistant text.
_TEXT_KINDS = frozenset({AgentEventKind.TEXT_DELTA})

# Event kinds that mark the end of a turn for budget accounting. AGENT_DONE
# fires once at the very end of the run and would over-count by one if
# included; TURN_COMPLETE is the canonical per-turn boundary.
_TURN_END_KINDS = frozenset({AgentEventKind.TURN_COMPLETE})


class TaskRunner:
    """Executes Tasks using an :class:`AgentLoopV2`.

    Each task gets a fresh execution context. The runner:
    1. Checks budget before executing
    2. Runs the agent loop with a task-specific prompt
    3. Collects the result summary
    4. Returns a TaskResult

    Usage::

        runner = TaskRunner(agent_loop, store, config)
        result = await runner.run(task, goal)
    """

    def __init__(
        self,
        agent_loop: AgentLoopV2,
        store: GoalStoreProtocol,
        config: KairosConfig,
    ) -> None:
        self._loop = agent_loop
        self._store = store
        self._config = config

    async def run(self, task: Task, goal: Goal) -> TaskResult:
        """Execute *task* within *goal* context.

        Handles retry logic internally. Returns final TaskResult
        (succeeded or failed — never raises for task-level failures).

        Raises:
            BudgetExceededError: If budget is exhausted before execution.
            InterventionRequiredError: If the task requires human input.
        """
        # Budget pre-check
        usage = self._store.get_budget_usage(goal.goal_id)
        exceeded = usage.exceeds(goal.budget)
        if exceeded:
            raise BudgetExceededError(
                f"Budget dimension '{exceeded}' exceeded before task {task.task_id}",
                dimension=exceeded,
                goal_id=goal.goal_id,
                task_id=task.task_id,
            )

        retry = task.retry_count
        last_error = ""

        while retry <= task.max_retries:
            result = await self._execute_once(task, goal, attempt=retry)

            if result.status == KairosTaskStatus.SUCCEEDED:
                return result

            last_error = result.error

            # Check if retryable
            if retry >= task.max_retries:
                logger.warning(
                    "Task %s failed after %d retries: %s",
                    task.task_id,
                    retry,
                    last_error,
                )
                return result  # Final FAILED result

            retry += 1
            self._store.update_task_status(
                task.task_id,
                KairosTaskStatus.RETRYING,
                retry_count=retry,
            )
            logger.info(
                "Retrying task %s (attempt %d/%d)",
                task.task_id,
                retry,
                task.max_retries,
            )
            # Brief backoff
            await asyncio.sleep(min(2.0**retry, 30.0))

        # Should not reach here, but return a failed result
        return TaskResult(
            task_id=task.task_id,
            goal_id=task.goal_id,
            plan_id=task.plan_id,
            status=KairosTaskStatus.FAILED,
            error=last_error or "Max retries exceeded",
        )

    async def _execute_once(self, task: Task, goal: Goal, attempt: int) -> TaskResult:
        """Single execution attempt for a task."""
        prompt = self._build_task_prompt(task, goal, attempt)
        start_ms = int(time.monotonic() * 1000)
        output_chunks: list[str] = []
        turns_used = 0
        tokens_used = 0

        try:
            async with asyncio.timeout(self._config.task_timeout_seconds):
                async for event in self._loop.run(
                    prompt,
                    session_id=f"kairos-{task.goal_id}-{task.task_id}",
                    max_turns=10,
                ):
                    if event.kind in _TEXT_KINDS and event.text:
                        output_chunks.append(event.text)
                    if event.kind in _TURN_END_KINDS:
                        turns_used += 1
                    usage_data = getattr(event, "usage", None)
                    if usage_data:
                        tokens_used += getattr(usage_data, "total_tokens", 0)

        except TimeoutError:
            logger.debug("suppressed exception in _execute_once", exc_info=True)
            elapsed = int(time.monotonic() * 1000) - start_ms
            return TaskResult(
                task_id=task.task_id,
                goal_id=task.goal_id,
                plan_id=task.plan_id,
                status=KairosTaskStatus.FAILED,
                error=f"Task timed out after {self._config.task_timeout_seconds}s",
                elapsed_ms=elapsed,
                turns_used=turns_used,
                tokens_used=tokens_used,
            )
        except Exception as exc:
            elapsed = int(time.monotonic() * 1000) - start_ms
            error_str = str(exc)
            logger.exception("Task %s execution error", task.task_id)
            return TaskResult(
                task_id=task.task_id,
                goal_id=task.goal_id,
                plan_id=task.plan_id,
                status=KairosTaskStatus.FAILED,
                error=error_str,
                elapsed_ms=elapsed,
                turns_used=turns_used,
                tokens_used=tokens_used,
            )

        elapsed = int(time.monotonic() * 1000) - start_ms
        output = "".join(output_chunks)

        # Update budget usage
        current_usage = self._store.get_budget_usage(goal.goal_id)
        new_usage = BudgetUsage(
            tasks_run=current_usage.tasks_run + 1,
            turns_used=current_usage.turns_used + turns_used,
            elapsed_seconds=current_usage.elapsed_seconds + (elapsed / 1000),
            tokens_used=current_usage.tokens_used + tokens_used,
            retries_used=current_usage.retries_used + (1 if attempt > 0 else 0),
        )
        self._store.update_budget_usage(goal.goal_id, new_usage)

        return TaskResult(
            task_id=task.task_id,
            goal_id=task.goal_id,
            plan_id=task.plan_id,
            status=KairosTaskStatus.SUCCEEDED,
            summary=output[:500],  # First 500 chars as summary
            output=output,
            turns_used=turns_used,
            tokens_used=tokens_used,
            elapsed_ms=elapsed,
        )

    def _build_task_prompt(self, task: Task, goal: Goal, attempt: int) -> str:
        retry_note = f"\n\n(Retry attempt {attempt})" if attempt > 0 else ""
        return (
            f"Goal: {goal.title}\n\n"
            f"Your current task: {task.title}\n\n"
            f"{task.description}"
            f"{retry_note}\n\n"
            "Complete this task. When done, summarize what you accomplished in 1-2 sentences."
        )
