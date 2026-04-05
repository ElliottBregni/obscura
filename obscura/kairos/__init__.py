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
"""

from obscura.kairos.away_summary import generate_away_summary
from obscura.kairos.daily_log import DailyLog
from obscura.kairos.dream import DreamConsolidator
from obscura.kairos.engine import KairosEngine
from obscura.kairos.frustration import FrustrationDetector
from obscura.kairos.goals import GoalBoard
from obscura.kairos.proactive import ProactiveMode
from obscura.kairos.undercover import UndercoverMode, is_undercover
from obscura.kairos.vault_sync import VaultSync

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
