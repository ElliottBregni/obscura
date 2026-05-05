"""obscura.arbiter.types — Frozen dataclass models for the Arbiter judge.

All types are frozen for thread-safety and immutability guarantees,
following the same pattern as ``obscura.eval.models``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast
import logging

logger = logging.getLogger(__name__)


class ArbiterVerdict(StrEnum):
    """Verdict the Arbiter can issue on an agent action."""

    ACCEPT = "accept"  # Proceed as-is.
    REVISE = "revise"  # Inject feedback, agent retries.
    DENY = "deny"  # Block this specific action.
    KILL = "kill"  # Abort the entire task/run.


class ArbiterCheckKind(StrEnum):
    """The type of agent action being evaluated."""

    TOOL_CALL = "tool_call"
    MODEL_TURN = "model_turn"
    TASK_COMPLETE = "task_complete"
    GOAL_TRANSITION = "goal_transition"


def _empty_tuple() -> tuple[str, ...]:
    return ()


def _empty_dict() -> dict[str, Any]:
    return {}


@dataclass(frozen=True)
class ArbiterScore:
    """Composite scoring result from the Arbiter pipeline."""

    deterministic: float  # 0.0-1.0
    judge: float | None = None  # 0.0-1.0 (normalized from 1-5 Likert)
    composite: float = 0.0
    verdict: ArbiterVerdict = ArbiterVerdict.ACCEPT
    feedback: str = ""
    check_kind: ArbiterCheckKind = ArbiterCheckKind.MODEL_TURN
    details: tuple[str, ...] = field(default_factory=_empty_tuple)


@dataclass(frozen=True)
class ArbiterConfig:
    """Configuration for the Arbiter engine.

    Loaded from env / settings.json at startup; immutable per session.

    *phantom_level* (-1=auto/env, 0=off, 1-5=shadow..takeover). Default -1 reads ``OBSCURA_PHANTOM_LEVEL``.
    At level 4+ (lead/takeover), non-safety verdicts are capped at
    REVISE — the agent gets steering feedback instead of hard blocks.
    Level 0 means phantom is off (normal Arbiter behavior).
    """

    enabled: bool = True
    judge_mode: str = "on_ambiguity"  # "always" | "on_ambiguity" | "never"
    accept_threshold: float = 0.8
    revise_threshold: float = 0.3
    max_retries: int = 2
    max_judge_calls_per_session: int = 15
    kill_on_safety_violation: bool = True
    phantom_level: int = -1  # -1=auto (read env), 0=off, 1-5=shadow..takeover
    is_daemon: bool = False  # True for daemon/background agents

    @classmethod
    def from_settings(cls, path: str | None = None) -> ArbiterConfig:
        """Load config from settings.json, env vars, then defaults.

        Settings file path: ``~/.obscura/settings.json`` (or *path*).
        JSON key: ``"arbiter"`` (dict with snake_case field names).
        Env prefix: ``OBSCURA_ARBITER_`` (e.g. ``OBSCURA_ARBITER_ENABLED``).
        """
        import json
        import os
        from pathlib import Path

        settings: dict[str, Any] = {}
        settings_path = (
            Path(path) if path else Path.home() / ".obscura" / "settings.json"
        )
        if settings_path.is_file():
            try:
                raw: Any = json.loads(settings_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    raw_dict = cast(dict[str, Any], raw)
                    arbiter_section = raw_dict.get("arbiter", {})
                    if isinstance(arbiter_section, dict):
                        settings = cast(dict[str, Any], arbiter_section)
            except (json.JSONDecodeError, OSError):
                logger.debug("suppressed exception in from_settings", exc_info=True)

        def _get(key: str, default: Any, caster: type) -> Any:
            # Settings file takes priority, then env, then default
            if key in settings:
                val: Any = settings[key]
                if caster is bool and isinstance(val, str):
                    return val.lower() in ("true", "1", "yes")
                return caster(val)
            env_key = f"OBSCURA_ARBITER_{key.upper()}"
            env_val = os.environ.get(env_key)
            if env_val is not None:
                if caster is bool:
                    return env_val.lower() in ("true", "1", "yes")
                return caster(env_val)
            return default

        return cls(
            enabled=_get("enabled", True, bool),
            judge_mode=_get("judge_mode", "on_ambiguity", str),
            accept_threshold=_get("accept_threshold", 0.8, float),
            revise_threshold=_get("revise_threshold", 0.3, float),
            max_retries=_get("max_retries", 2, int),
            max_judge_calls_per_session=_get("max_judge_calls_per_session", 15, int),
            kill_on_safety_violation=_get("kill_on_safety_violation", True, bool),
            phantom_level=_get("phantom_level", -1, int),
            is_daemon=_get("is_daemon", False, bool),
        )


@dataclass(frozen=True)
class ArbiterEvent:
    """A single Arbiter evaluation event for the audit log."""

    kind: ArbiterCheckKind
    verdict: ArbiterVerdict
    score: ArbiterScore
    target_id: str = ""  # task_id, tool_name, goal_id
    session_id: str = ""
    run_id: str = ""
    retry_count: int = 0
    metadata: dict[str, Any] = field(default_factory=_empty_dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
