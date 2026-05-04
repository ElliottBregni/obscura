"""obscura.core.agent_loop_hooks — pre_turn / post_turn hook builders for AgentLoopV2.

These wrap turn-level concerns (compaction, arbiter eval) into the simple
``Callable`` shape AgentLoopV2 accepts via the ``pre_turn`` / ``post_turn``
constructor kwargs.

============================  ============================  =========================
v1 ``AgentLoop`` kwarg        v2 hook builder               When it fires
============================  ============================  =========================
context_budget + compaction   :func:`compact_pre_turn`      Before each model stream
arbiter (turn-level)          :func:`arbiter_post_turn`     After tools complete
event_store persistence       :func:`event_store_post_turn` After tools complete
============================  ============================  =========================
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from obscura.core.agent_loop_v2 import TurnContext, TurnResult


logger = logging.getLogger(__name__)


__all__ = [
    "arbiter_post_turn",
    "compact_pre_turn",
    "event_store_post_turn",
]


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def compact_pre_turn(
    *,
    model_id: str,
    system_prompt: str = "",
    max_history_share: float = 0.5,
    reserve_tokens: int = 4096,
    only_when_over_threshold: bool = True,
) -> Callable[[TurnContext], Awaitable[None]]:
    """Build a ``pre_turn`` hook that runs :func:`compact_history` before each turn.

    Mirrors v1's between-turn compaction. The hook mutates ``ctx.messages``
    in-place. By default, compaction only runs when the message history
    is over the threshold; set ``only_when_over_threshold=False`` to
    always run (mostly useful for debugging).
    """

    async def hook(ctx: TurnContext) -> None:
        # Lazy-import to avoid pulling compaction into ``import obscura.core``.
        from obscura.core.compaction import compact_history

        compacted, was_compacted, _ = await compact_history(
            ctx.messages,
            model_id=model_id,
            system_prompt=system_prompt,
            max_history_share=max_history_share,
            reserve_tokens=reserve_tokens,
        )
        if was_compacted:
            logger.info(
                "compact_pre_turn: turn %d trimmed %d → %d messages",
                ctx.turn_index,
                len(ctx.messages),
                len(compacted),
            )
            ctx.messages[:] = compacted
        elif not only_when_over_threshold:
            ctx.messages[:] = compacted

    return hook


# ---------------------------------------------------------------------------
# Arbiter (turn-level)
# ---------------------------------------------------------------------------


def arbiter_post_turn(
    arbiter: Any,
    *,
    kill_on_fail: bool = True,
) -> Callable[[TurnContext, TurnResult], Awaitable[None]]:
    """Build a ``post_turn`` hook that runs the arbiter against the latest turn.

    *arbiter* is opaque — pass a v1-style arbiter (anything with an
    ``evaluate(messages)`` method, async or sync). When *kill_on_fail* is
    True (default), a fail result sets ``ctx.stop_after_turn = True`` so
    the loop terminates after this turn.

    Mirrors v1's arbiter integration which set ``self._arbiter_killed``
    and broke out of the run loop.
    """

    async def hook(ctx: TurnContext, _result: TurnResult) -> None:
        evaluate = getattr(arbiter, "evaluate", None)
        if evaluate is None:
            return
        try:
            decision = evaluate(ctx.messages)
            if hasattr(decision, "__await__"):
                decision = await decision
        except Exception:
            logger.exception("arbiter raised in post_turn — swallowing")
            return

        passed = bool(getattr(decision, "passed", True))
        if not passed and kill_on_fail:
            reason = getattr(decision, "reason", "arbiter fail")
            logger.info(
                "arbiter_post_turn: kill at turn %d (%s)", ctx.turn_index, reason
            )
            ctx.stop_after_turn = True

    return hook


# ---------------------------------------------------------------------------
# Event store persistence
# ---------------------------------------------------------------------------


def event_store_post_turn(
    event_store: Any,
    *,
    session_id: str,
) -> Callable[[TurnContext, TurnResult], Awaitable[None]]:
    """Build a ``post_turn`` hook that records turn results to an event store.

    Mirrors v1's ``AgentLoop(event_store=...)`` integration. *event_store*
    must implement ``EventStoreProtocol.append(session_id, AgentEvent)``.
    Each turn produces one ``TURN_COMPLETE`` record with the assistant
    text; tool-call / result counts are stashed in ``raw`` for any
    downstream consumer that wants them.
    """
    from obscura.core.enums.agent import AgentEventKind
    from obscura.core.types import AgentEvent

    async def hook(ctx: TurnContext, result: TurnResult) -> None:
        append = getattr(event_store, "append", None)
        if append is None:
            return
        event = AgentEvent(
            kind=AgentEventKind.TURN_COMPLETE,
            text=result.text,
            turn=ctx.turn_index,
            raw={
                "tool_calls": len(result.tool_calls),
                "results": len(result.results),
            },
        )
        try:
            r = append(session_id, event)
            if hasattr(r, "__await__"):
                await r
        except Exception:
            logger.exception("event_store.append raised in post_turn — swallowing")

    return hook
