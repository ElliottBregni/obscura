"""KairosAgentRunner — routes long-horizon channel messages into the KAIROS goal runtime.

This module bridges the messaging layer (ChannelRouter) with the KAIROS autonomous
goal runtime.  It implements AgentRunnerProtocol so it is a drop-in replacement for
ObscuraAgentRunner inside ChannelRouter.

Routing logic
-------------
Each inbound message is classified as either *immediate* or *deferred*:

* **Immediate** — short Q&A, greetings, status questions, quick commands.
  Handled by a standard AgentLoop turn (identical to ObscuraAgentRunner).

* **Deferred** — long-horizon autonomous work: research, audits, multi-step
  analysis, write-and-report tasks.  These are handed to Kairos which
  creates a durable Goal, decomposes it into Tasks, and executes them with
  checkpointing and budget enforcement.  Progress events are folded into a
  human-readable reply that is returned to the caller once the goal finishes
  (or hits its budget).

Configuration
-------------
    from obscura.integrations.messaging.kairos_runner import KairosAgentRunner, KairosRunnerConfig
    from obscura.integrations.messaging.router import ChannelRouter, ChannelRouterConfig

    runner = KairosAgentRunner(
        backend=client.backend,
        tool_registry=client.tool_registry,
        config=KairosRunnerConfig(
            db_path="~/.obscura/kairos.db",
            budget_turns=40,
            budget_wall_seconds=300,
        ),
    )
    channel_router = ChannelRouter(runner=runner, config=ChannelRouterConfig(...))

Intent classification
---------------------
Intent is detected by a lightweight keyword + length heuristic — no extra
LLM call is needed.  The threshold is intentionally conservative: ambiguous
messages default to immediate handling to keep conversational turns snappy.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from obscura.core.agent_loop_factory import make_agent_loop
from obscura.core.kairos import GoalBudget, Kairos
from obscura.core.kairos.types import KairosEventKind
from obscura.integrations.messaging.runners import ObscuraAgentRunner

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keywords that signal a long-horizon autonomous task
# ---------------------------------------------------------------------------

_DEFERRED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(research|investigate|audit|survey|analyse|analyze)\b", re.I),
    re.compile(r"\b(compile|gather|collect|assemble|aggregate)\b", re.I),
    re.compile(r"\b(find all|list all|enumerate|map out|catalog)\b", re.I),
    re.compile(r"\b(write (a |an )?(report|summary|brief|analysis|memo))\b", re.I),
    re.compile(r"\b(monitor|track|watch|keep (an )?eye on)\b", re.I),
    re.compile(r"\b(run (an? )?(analysis|report|audit|scan|review))\b", re.I),
    re.compile(r"\b(go (through|over)|review (all|every|each))\b", re.I),
]

# Minimum prompt word-count before deferred routing is considered
_MIN_DEFERRED_WORDS = 8


def _classify_prompt(text: str) -> str:
    """Return 'deferred' if the prompt looks like a long-horizon task, else 'immediate'."""
    words = text.split()
    if len(words) < _MIN_DEFERRED_WORDS:
        return "immediate"
    for pattern in _DEFERRED_PATTERNS:
        if pattern.search(text):
            return "deferred"
    return "immediate"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class KairosRunnerConfig:
    """Configuration for KairosAgentRunner."""

    db_path: str | Path = field(
        default_factory=lambda: Path.home() / ".obscura" / "kairos.db"
    )
    # Budget applied to every goal spawned from a channel message.
    # 0 = unlimited (not recommended for channel use).
    budget_turns: int = 40
    budget_tasks: int = 20
    budget_wall_seconds: float = 240.0
    # Fallback: run an immediate AgentLoop turn when Kairos fails to initialise.
    fallback_on_error: bool = True
    # Max characters for the inline progress summary returned to the user.
    max_summary_chars: int = 1200


# ---------------------------------------------------------------------------
# KairosAgentRunner
# ---------------------------------------------------------------------------


class KairosAgentRunner:
    """Drop-in AgentRunnerProtocol implementation that routes deferred requests to KAIROS.

    For immediate requests it delegates to a standard ObscuraAgentRunner.
    """

    def __init__(
        self,
        backend: Any,
        tool_registry: Any,
        *,
        config: KairosRunnerConfig | None = None,
        event_store: Any | None = None,
    ) -> None:
        self._backend = backend
        self._tool_registry = tool_registry
        self._config = config or KairosRunnerConfig()
        self._event_store = event_store

    # ------------------------------------------------------------------
    # AgentRunnerProtocol
    # ------------------------------------------------------------------

    async def run_turn(
        self,
        prompt: str,
        *,
        session_id: str,
        history: list[dict[str, str]],
        system_prompt: str,
        max_turns: int,
    ) -> str:
        intent = _classify_prompt(prompt)
        if intent == "deferred":
            logger.info(
                "KairosAgentRunner: routing deferred goal for session=%s", session_id
            )
            try:
                return await self._run_as_goal(
                    prompt,
                    session_id=session_id,
                    system_prompt=system_prompt,
                )
            except Exception:
                logger.exception(
                    "KairosAgentRunner: goal execution failed for session=%s — %s",
                    session_id,
                    "falling back to immediate"
                    if self._config.fallback_on_error
                    else "re-raising",
                )
                if not self._config.fallback_on_error:
                    raise

        # Immediate path (or fallback after goal error)
        return await self._run_immediate(
            prompt,
            session_id=session_id,
            history=history,
            system_prompt=system_prompt,
            max_turns=max_turns,
        )

    # ------------------------------------------------------------------
    # Immediate path
    # ------------------------------------------------------------------

    async def _run_immediate(
        self,
        prompt: str,
        *,
        session_id: str,
        history: list[dict[str, str]],
        system_prompt: str,
        max_turns: int,
    ) -> str:
        runner = ObscuraAgentRunner(
            backend=self._backend,
            tool_registry=self._tool_registry,
            event_store=self._event_store,
        )
        return await runner.run_turn(
            prompt,
            session_id=session_id,
            history=history,
            system_prompt=system_prompt,
            max_turns=max_turns,
        )

    # ------------------------------------------------------------------
    # Deferred / KAIROS path
    # ------------------------------------------------------------------

    async def _run_as_goal(
        self,
        prompt: str,
        *,
        session_id: str,
        system_prompt: str,
    ) -> str:
        cfg = self._config
        loop = make_agent_loop(
            backend=self._backend,
            tool_registry=self._tool_registry,
        )
        kairos = Kairos(
            db_path=cfg.db_path,
            agent_loop=loop,
        )

        budget = GoalBudget(
            max_turns=cfg.budget_turns,
            max_tasks=cfg.budget_tasks,
            max_wall_seconds=cfg.budget_wall_seconds,
        )

        try:
            goal_id = await kairos.create_goal(
                title=prompt[:200],
                description=prompt,
                session_id=session_id,
                budget=budget,
                metadata={"source": "channel", "session_id": session_id},
            )

            lines: list[str] = []
            tasks_done = 0
            tasks_failed = 0
            final_status = "completed"

            async for event in kairos.run(goal_id):
                kind = event.kind
                p = event.payload

                if kind == KairosEventKind.PLAN_CREATED:
                    n = p.get("task_count", "?")
                    lines.append(f"📋 Plan ready — {n} task(s) to run")

                elif kind == KairosEventKind.TASK_STARTED:
                    title = p.get("title", "")
                    if title:
                        lines.append(f"  → {title}")

                elif kind == KairosEventKind.TASK_SUCCEEDED:
                    tasks_done += 1

                elif kind == KairosEventKind.TASK_FAILED:
                    tasks_failed += 1
                    err = str(p.get("error", ""))[:80]
                    lines.append(f"  ✗ {err}")

                elif kind == KairosEventKind.INTERVENTION_RAISED:
                    question = p.get("question", "")
                    iid = str(p.get("intervention_id", ""))[:12]
                    lines.append(
                        f"⚠ Need your input (id: {iid}): {question}\n"
                        f"Reply: /kairos respond {goal_id} {iid} <answer>"
                    )
                    final_status = "blocked"
                    break

                elif kind == KairosEventKind.BUDGET_EXCEEDED:
                    dim = p.get("dimension", "")
                    lines.append(f"⛔ Budget exceeded ({dim}) — stopping here")
                    final_status = "budget"
                    break

                elif kind in (
                    KairosEventKind.GOAL_COMPLETED,
                    KairosEventKind.GOAL_FAILED,
                    KairosEventKind.GOAL_CANCELLED,
                ):
                    if kind == KairosEventKind.GOAL_FAILED:
                        final_status = "failed"
                        err = str(p.get("error", ""))[:120]
                        lines.append(f"✗ {err}")
                    break

            # Build the reply
            summary_parts: list[str] = []

            if final_status == "completed":
                count_str = (
                    f"{tasks_done} task{'s' if tasks_done != 1 else ''} completed"
                )
                if tasks_failed:
                    count_str += f", {tasks_failed} failed"
                summary_parts.append(f"✓ Done ({count_str})")
            elif final_status == "failed":
                summary_parts.append("✗ Goal could not be completed.")
            elif final_status == "budget":
                summary_parts.append(
                    f"⏱ Stopped at budget limit ({tasks_done} tasks done)."
                )

            if lines:
                step_log = "\n".join(lines)
                if len(step_log) > cfg.max_summary_chars:
                    step_log = step_log[: cfg.max_summary_chars] + "\n…(truncated)"
                summary_parts.append(step_log)

            return "\n".join(summary_parts).strip() or "(goal finished with no output)"

        finally:
            await kairos.close()
