"""obscura.kairos.proactive — Tick-based proactive mode.

In proactive mode, the agent receives periodic ``<tick>`` prompts
and can take autonomous actions without waiting for user input.
A 15-second blocking budget prevents proactive actions from
interrupting the user's workflow.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

logger = logging.getLogger(__name__)

# Maximum time a proactive action can block user input.
PROACTIVE_BLOCKING_BUDGET_S = 15.0

# Interval between tick prompts (seconds).
TICK_INTERVAL_S = 60.0

PROACTIVE_SYSTEM_PROMPT = """\
# Proactive Mode

You are in proactive mode. Take initiative — explore, act, and make
progress without waiting for instructions.

You will receive periodic <tick> prompts. These are check-ins.
Each tick may include a `focus=<goal-id>(<progress>%)` hint pointing
to the highest-priority unblocked goal.

On each tick:
1. Check if there's a focus goal — if so, take one small concrete
   action toward it (run a test, read a file, write a fix)
2. If no focus goal, look for other useful work (fix warnings, update
   docs, clean up TODOs)
3. If nothing actionable, call Sleep
4. Always log what you did to the daily log

Rules:
- Keep proactive actions under 15 seconds of blocking time
- Don't interrupt the user if they're actively working
- Log significant observations to the daily log
- Don't thrash — if you worked on a goal in the last tick, give it
  time to settle before revisiting
"""


def is_proactive_enabled() -> bool:
    """Check if proactive mode is enabled (Kairos subsystem)."""
    val = os.environ.get("OBSCURA_KAIROS_PROACTIVE", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def set_proactive_mode(enabled: bool) -> None:
    """Enable or disable proactive mode (Kairos subsystem)."""
    os.environ["OBSCURA_KAIROS_PROACTIVE"] = "1" if enabled else "0"


class ProactiveMode:
    """Manages tick-based proactive actions.

    Usage::

        proactive = ProactiveMode(agent_loop)
        await proactive.start()
        # ... agent receives <tick> prompts periodically ...
        await proactive.stop()
    """

    def __init__(
        self,
        on_tick: object | None = None,
        tick_interval: float = TICK_INTERVAL_S,
    ) -> None:
        self._on_tick = on_tick
        self._tick_interval = tick_interval
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._last_tick = 0.0
        self._tick_count = 0

    async def start(self) -> None:
        """Start the proactive tick loop."""
        self._stopped = False
        self._task = asyncio.create_task(self._tick_loop())
        logger.debug("Proactive mode started (interval=%ss)", self._tick_interval)

    async def stop(self) -> None:
        """Stop the proactive tick loop."""
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.debug("Proactive mode stopped after %d ticks", self._tick_count)

    async def _tick_loop(self) -> None:
        """Periodically fire tick events for proactive monitoring."""
        while not self._stopped:
            await asyncio.sleep(self._tick_interval)
            if self._stopped:
                break
            self._tick_count += 1
            self._last_tick = time.time()

            # Log tick to daily log.
            try:
                from obscura.kairos.daily_log import DailyLog

                DailyLog().append(f"tick #{self._tick_count}", source="proactive")
            except Exception:
                pass

            # Fire on_tick callback if set.
            if self._on_tick is not None:
                try:
                    result = self._on_tick(self._tick_count)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    logger.debug("Proactive tick callback failed", exc_info=True)

            # Log to deep log.
            try:
                from obscura.core.deep_log import dlog

                dlog.event("proactive_tick", tick=self._tick_count)
            except Exception:
                pass

            logger.debug("Proactive tick #%d", self._tick_count)

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._stopped

    @property
    def tick_count(self) -> int:
        return self._tick_count

    def get_system_prompt_addition(self) -> str:
        """Return the proactive mode system prompt addition."""
        return PROACTIVE_SYSTEM_PROMPT
