"""
obscura.kairos.engine — KAIROS daemon engine.

The main orchestrator for KAIROS mode: combines daily logging,
proactive ticks, dream consolidation, and background monitoring
into a single daemon lifecycle.
"""

from __future__ import annotations

import logging
import os
import time

from obscura.kairos.daily_log import DailyLog
from obscura.kairos.proactive import ProactiveMode

logger = logging.getLogger(__name__)


def is_kairos_enabled() -> bool:
    """Check if KAIROS mode is enabled."""
    val = os.environ.get("OBSCURA_KAIROS", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def set_kairos_mode(enabled: bool) -> None:
    """Enable or disable KAIROS mode."""
    os.environ["OBSCURA_KAIROS"] = "1" if enabled else ""


class KairosEngine:
    """KAIROS daemon engine — watches, logs, and acts autonomously.

    Combines:
      - Daily append-only logging
      - Proactive tick-based actions
      - Dream consolidation scheduling
      - Background session monitoring

    Usage::

        engine = KairosEngine()
        await engine.start()
        engine.log("User started working on auth refactor")
        # ... engine runs in background ...
        await engine.stop()
    """

    def __init__(
        self,
        *,
        tick_interval: float = 60.0,
        dream_min_hours: float = 24.0,
        dream_min_sessions: int = 5,
    ) -> None:
        self._daily_log = DailyLog()
        self._proactive = ProactiveMode(tick_interval=tick_interval)
        self._dream_min_hours = dream_min_hours
        self._dream_min_sessions = dream_min_sessions
        self._started = False
        self._start_time = 0.0
        self._observation_count = 0

    @property
    def is_running(self) -> bool:
        return self._started

    async def start(self) -> None:
        """Start the KAIROS engine."""
        if self._started:
            return
        self._started = True
        self._start_time = time.time()

        # Log engine start.
        self.log("KAIROS engine started")

        # Start proactive tick loop.
        await self._proactive.start()

        logger.info("KAIROS engine started")

    async def stop(self) -> None:
        """Stop the KAIROS engine."""
        if not self._started:
            return

        await self._proactive.stop()
        self.log("KAIROS engine stopped")
        self._started = False

        # Check if dream consolidation should run.
        await self._maybe_dream()

        logger.info(
            "KAIROS engine stopped after %.0fs, %d observations",
            time.time() - self._start_time,
            self._observation_count,
        )

    def log(self, entry: str, *, source: str = "kairos") -> None:
        """Append an observation to the daily log."""
        self._daily_log.append(entry, source=source)
        self._observation_count += 1

    def log_tool_use(self, tool_name: str, args_summary: str) -> None:
        """Log a tool invocation to the daily log."""
        self.log(f"tool:{tool_name} — {args_summary}", source="tool")

    def log_user_message(self, message_preview: str) -> None:
        """Log a user message to the daily log."""
        preview = message_preview[:100].replace("\n", " ")
        self.log(f"user: {preview}", source="user")

    def log_agent_event(self, event_kind: str, detail: str = "") -> None:
        """Log an agent event to the daily log."""
        self.log(f"event:{event_kind} {detail}".strip(), source="agent")

    async def _maybe_dream(self) -> None:
        """Check if dream consolidation should run."""
        from obscura.kairos.dream import DreamConsolidator

        consolidator = DreamConsolidator(
            min_hours=self._dream_min_hours,
            min_sessions=self._dream_min_sessions,
        )
        if consolidator.should_run():
            logger.info("Starting dream consolidation...")
            self.log("Dream consolidation triggered")
            await consolidator.run()
            self.log("Dream consolidation completed")

    def get_system_prompt_addition(self) -> str:
        """Return KAIROS system prompt additions."""
        parts = [
            "# KAIROS Mode Active\n",
            "You are in KAIROS mode — an autonomous background daemon.",
            "You maintain daily logs of observations and can act proactively.",
            "",
            "Behaviors:",
            "- Log significant observations (file changes, errors, patterns)",
            "- Act on pending tasks during idle periods",
            "- Consolidate memories during dream cycles",
            "- Respect the 15-second blocking budget for proactive actions",
        ]
        if self._proactive.is_running:
            parts.append("")
            parts.append(self._proactive.get_system_prompt_addition())
        return "\n".join(parts)

    def status(self) -> dict[str, object]:
        """Return engine status for diagnostics."""
        return {
            "running": self._started,
            "uptime_s": time.time() - self._start_time if self._started else 0,
            "observations": self._observation_count,
            "tick_count": self._proactive.tick_count,
            "daily_log_entries": self._daily_log.entry_count(),
            "daily_log_path": str(self._daily_log.path),
        }
