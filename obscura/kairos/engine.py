"""obscura.kairos.engine — KAIROS daemon engine.

The main orchestrator for KAIROS mode: combines daily logging,
proactive ticks, dream consolidation, and background monitoring
into a single daemon lifecycle.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from obscura.kairos.daily_log import DailyLog
from obscura.kairos.proactive import ProactiveMode

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool = True) -> bool:
    """Read a boolean env var. Default is True (opt-out pattern)."""
    val = os.environ.get(name, "").strip().lower()
    if val in ("0", "false", "no", "off"):
        return False
    if val in ("1", "true", "yes", "on"):
        return True
    return default


def _settings_flag(key: str, default: bool = True) -> bool:
    """Read a boolean toggle from ~/.obscura/settings.json if present.

    Supports nested keys via dot-notation (e.g., "kairos.enabled"). If the
    key is missing or the file is unreadable, returns ``default``.
    """
    try:
        settings_path = Path.home() / ".obscura" / "settings.json"
        if not settings_path.is_file():
            return default
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        cur: object = data
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:  # type: ignore[redundant-cast]
                cur = cur[part]  # type: ignore[index]
            else:
                return default
        if isinstance(cur, bool):
            return cur
        if isinstance(cur, str):
            val = cur.strip().lower()
            if val in ("0", "false", "no", "off"):
                return False
            if val in ("1", "true", "yes", "on"):
                return True
        return default
    except Exception:
        return default


def is_kairos_enabled() -> bool:
    """Check if KAIROS mode is enabled (default: on).

    Resolution order (opt-out):
      1) ~/.obscura/settings.json → key "kairos.enabled"
      2) OBSCURA_KAIROS env var
      3) default True
    """
    return _settings_flag("kairos.enabled", _env_flag("OBSCURA_KAIROS", default=True))


def set_kairos_mode(enabled: bool) -> None:
    """Enable or disable KAIROS mode."""
    os.environ["OBSCURA_KAIROS"] = "1" if enabled else "0"


class KairosEngine:
    """KAIROS daemon engine — watches, logs, and acts autonomously.

    Combines:
      - Daily append-only logging
      - Proactive tick-based actions (opt-out: OBSCURA_KAIROS_PROACTIVE=false)
      - Dream consolidation scheduling (opt-out: OBSCURA_KAIROS_DREAM=false)
      - Background session monitoring

    Cost-reduction env vars:
      - OBSCURA_KAIROS=false          Disable entire daemon
      - OBSCURA_KAIROS_PROACTIVE=false Disable tick loop (saves tokens)
      - OBSCURA_KAIROS_DREAM=false     Disable dream consolidation (saves tokens)

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
        # Cost-reduction toggles (None = read from env)
        proactive_enabled: bool | None = None,
        dream_enabled: bool | None = None,
    ) -> None:
        self._proactive_enabled = (
            proactive_enabled
            if proactive_enabled is not None
            else _settings_flag(
                "kairos.proactive",
                _env_flag("OBSCURA_KAIROS_PROACTIVE", default=True),
            )
        )
        self._dream_enabled = (
            dream_enabled
            if dream_enabled is not None
            else _settings_flag(
                "kairos.dream",
                _env_flag("OBSCURA_KAIROS_DREAM", default=True),
            )
        )

        self._daily_log = DailyLog()
        self._active_loop: object | None = None
        self._proactive: ProactiveMode | None = (
            ProactiveMode(
                on_tick=self._on_proactive_tick,
                tick_interval=tick_interval,
            )
            if self._proactive_enabled
            else None
        )
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

        # Start proactive tick loop if enabled.
        if self._proactive is not None:
            await self._proactive.start()

        logger.info(
            "KAIROS engine started (proactive=%s dream=%s)",
            self._proactive_enabled,
            self._dream_enabled,
        )

    async def stop(self) -> None:
        """Stop the KAIROS engine."""
        if not self._started:
            return

        if self._proactive is not None:
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

    def register_agent_loop(self, loop: object) -> None:
        """Attach the active AgentLoop for proactive tick injection.

        Call this after the ObscuraClient/AgentLoop has started. The loop's
        ``inject_user_input()`` will receive ``<tick>`` messages on each
        proactive tick so the model can act within the 15s blocking budget.
        """
        self._active_loop = loop
        logger.debug("KairosEngine: AgentLoop registered for tick injection")

    def _on_proactive_tick(self, tick_count: int) -> None:
        """Callback fired by ProactiveMode on each tick.

        Injects a ``<tick>`` prompt into the active AgentLoop so the model
        can take a proactive action without waiting for user input.
        Includes the top-priority goal hint so the model has fresh context.
        """
        loop = self._active_loop
        if loop is None:
            return
        try:
            inject = getattr(loop, "inject_user_input", None)
            if callable(inject):
                goal_hint = ""
                try:
                    from obscura.kairos.goals import GoalBoard

                    top = GoalBoard().active_goals()
                    top = [g for g in top if not g.is_blocked][:1]
                    if top:
                        goal_hint = f" focus={top[0].id}({top[0].progress}%)"
                except Exception:
                    pass
                inject(f"<tick>#{tick_count}{goal_hint}</tick>")
        except Exception:
            logger.debug("Proactive tick injection failed", exc_info=True)

    async def _maybe_dream(self) -> None:
        """Check if dream consolidation should run."""
        if not self._dream_enabled:
            return
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
        """Return KAIROS system prompt additions (including undercover instructions)."""
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
            "- Work toward active goals on the goal board",
        ]

        # Inject active goal summary.
        try:
            from obscura.kairos.goals import GoalBoard

            summary = GoalBoard().active_summary(max_lines=8)
            if summary:
                parts.append("")
                parts.append("## Active Goals")
                parts.append(summary)
                parts.append("")
                parts.append(
                    "When receiving <tick> prompts, prioritize the highest-priority "
                    "unblocked goal. Take one small, concrete action per tick."
                )
        except Exception:
            pass
        if self._proactive is not None and self._proactive.is_running:
            parts.append("")
            parts.append(self._proactive.get_system_prompt_addition())

        # Inject undercover instructions if active.
        try:
            from obscura.kairos.undercover import UndercoverMode

            uc_prompt = UndercoverMode().get_system_prompt_addition()
            if uc_prompt:
                parts.append("")
                parts.append(uc_prompt)
        except Exception:
            pass

        return "\n".join(parts)

    def status(self) -> dict[str, object]:
        """Return engine status for diagnostics."""
        return {
            "running": self._started,
            "uptime_s": time.time() - self._start_time if self._started else 0,
            "observations": self._observation_count,
            "tick_count": self._proactive.tick_count if self._proactive else 0,
            "proactive_enabled": self._proactive_enabled,
            "dream_enabled": self._dream_enabled,
            "daily_log_entries": self._daily_log.entry_count(),
            "daily_log_path": str(self._daily_log.path),
        }
