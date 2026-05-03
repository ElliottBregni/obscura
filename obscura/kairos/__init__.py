"""obscura.kairos — Proactive daemon mode with autonomous monitoring.

KAIROS is a background agent layer that watches, logs, and acts
autonomously. It maintains daily append-only logs of observations,
receives periodic tick prompts, and can trigger proactive actions.

Key components:
  - ``KairosEngine``: Main daemon loop with tick-based scheduling
  - ``DailyLog``: Append-only daily log management
  - ``DreamConsolidator``: Memory consolidation during idle periods
  - ``ProactiveMode``: Tick-based autonomous action system
  - ``UndercoverMode``: Strip AI attribution in public repos
  - ``AwaySummary``: Summarize what happened while user was away
  - ``FrustrationDetector``: Detect user frustration for UX adaptation

Layer note
----------
``KairosEngine``, ``DreamConsolidator`` and ``VaultSync`` are heavyweight:
they reach into ``obscura.tools.*`` (engine via ``arbiter.watchdog``
which lazy-imports ``kairos.goals``; dream directly imports
``tools.profile_tools`` / ``tools.goal_tools`` / ``tools.system``;
vault_sync pulls in arbiter, profile, and vector_memory).

Eagerly re-exporting them from this ``__init__`` made every consumer of
the leaf modules pay the cost — and forced ``obscura.tools.profile_tools``
/ ``obscura.tools.goal_tools`` / ``obscura.arbiter.watchdog`` to
lazy-import ``kairos.*`` to avoid partial-init cycles.

Resolution: leaves stay eager; ``KairosEngine`` / ``DreamConsolidator``
/ ``VaultSync`` load lazily via ``__getattr__`` only when accessed.
``from obscura.kairos import KairosEngine`` still works — it just
triggers the heavy import at access time, not at package import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from obscura.kairos.daily_log import DailyLog
from obscura.kairos.frustration import FrustrationDetector
from obscura.kairos.goals import GoalBoard
from obscura.kairos.proactive import ProactiveMode
from obscura.kairos.undercover import UndercoverMode, is_undercover

if TYPE_CHECKING:
    # Type-checkers see these names so static
    # ``from obscura.kairos import KairosEngine`` resolves correctly.
    # At runtime ``__getattr__`` below loads the relevant submodule on
    # first access to break the cycle through ``obscura.tools.*``.
    from obscura.kairos.away_summary import generate_away_summary
    from obscura.kairos.dream import DreamConsolidator
    from obscura.kairos.engine import KairosEngine
    from obscura.kairos.vault_sync import VaultSync


_LAZY_FROM_AWAY_SUMMARY = frozenset({"generate_away_summary"})
_LAZY_FROM_DREAM = frozenset({"DreamConsolidator"})
_LAZY_FROM_ENGINE = frozenset({"KairosEngine"})
_LAZY_FROM_VAULT_SYNC = frozenset({"VaultSync"})

_ALL_LAZY = (
    _LAZY_FROM_AWAY_SUMMARY
    | _LAZY_FROM_DREAM
    | _LAZY_FROM_ENGINE
    | _LAZY_FROM_VAULT_SYNC
)


def __getattr__(name: str) -> Any:
    if name in _LAZY_FROM_DREAM:
        from obscura.kairos import dream as _dream

        return getattr(_dream, name)
    if name in _LAZY_FROM_ENGINE:
        from obscura.kairos import engine as _engine

        return getattr(_engine, name)
    if name in _LAZY_FROM_VAULT_SYNC:
        from obscura.kairos import vault_sync as _vault_sync

        return getattr(_vault_sync, name)
    if name in _LAZY_FROM_AWAY_SUMMARY:
        from obscura.kairos import away_summary as _away_summary

        return getattr(_away_summary, name)
    raise AttributeError(f"module 'obscura.kairos' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _ALL_LAZY)


__all__ = [
    "DailyLog",
    "DreamConsolidator",
    "FrustrationDetector",
    "GoalBoard",
    "KairosEngine",
    "ProactiveMode",
    "UndercoverMode",
    "VaultSync",
    "generate_away_summary",
    "is_undercover",
]
