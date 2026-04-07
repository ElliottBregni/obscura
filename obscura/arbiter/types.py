"""obscura.arbiter.types — Frozen dataclass models for the Arbiter judge.

All types are frozen for thread-safety and immutability guarantees,
following the same pattern as ``obscura.eval.models``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


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

    *phantom_level* (0-5) is read from ``OBSCURA_PHANTOM_LEVEL``.
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
    phantom_level: int = 0  # 0=off, 1-5 = shadow..takeover
    is_daemon: bool = False  # True for daemon/background agents


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
