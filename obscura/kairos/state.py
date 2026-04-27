"""obscura.kairos.state — Cross-session state persistence for KAIROS.

Persists KAIROS state to ``~/.obscura/kairos_state.json`` so that
key metrics and learning survive across sessions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path.home() / ".obscura" / "kairos_state.json"


@dataclass
class KairosState:
    """Persistent KAIROS state across sessions.

    Tracks session counts, dream timestamps, proactive tick history,
    and cross-session learning signals.
    """

    # Session tracking
    total_sessions: int = 0
    last_session_id: str = ""
    last_session_start: str = ""
    last_session_end: str = ""

    # Dream consolidation
    last_dream_timestamp: str = ""
    dream_count: int = 0

    # Proactive ticks
    total_proactive_ticks: int = 0
    last_proactive_tick: str = ""

    # Cross-session learning
    common_errors: dict[str, int] = field(default_factory=dict)
    project_roots_seen: list[str] = field(default_factory=list)

    # Daily log stats
    total_log_entries: int = 0
    last_log_date: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> KairosState:
        """Load state from disk, returning defaults if not found."""
        state_path = path or _DEFAULT_PATH
        if not state_path.is_file():
            return cls()
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            # Only use known fields to avoid breakage on schema changes
            known = {f.name for f in cls.__dataclass_fields__.values()}
            filtered = {k: v for k, v in raw.items() if k in known}
            state = cls(**filtered)
            # Enforce caps on load so old bloated state files are cleaned up
            state.project_roots_seen = list(dict.fromkeys(state.project_roots_seen))[-100:]
            if len(state.common_errors) > 50:
                sorted_errors = sorted(
                    state.common_errors.items(), key=lambda x: x[1], reverse=True,
                )
                state.common_errors = dict(sorted_errors[:50])
            return state
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            logger.warning("Failed to load KAIROS state: %s", exc)
            return cls()

    def save(self, path: Path | None = None) -> None:
        """Persist state to disk."""
        state_path = path or _DEFAULT_PATH
        state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            state_path.write_text(
                json.dumps(asdict(self), indent=2, default=str),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Failed to save KAIROS state: %s", exc)

    def record_session_start(self, session_id: str) -> None:
        """Record a new session starting."""
        self.total_sessions += 1
        self.last_session_id = session_id
        self.last_session_start = datetime.now(UTC).isoformat()

    def record_session_end(self) -> None:
        """Record the current session ending."""
        self.last_session_end = datetime.now(UTC).isoformat()

    def record_dream(self) -> None:
        """Record a dream consolidation run."""
        self.dream_count += 1
        self.last_dream_timestamp = datetime.now(UTC).isoformat()

    def record_proactive_tick(self) -> None:
        """Record a proactive tick."""
        self.total_proactive_ticks += 1
        self.last_proactive_tick = datetime.now(UTC).isoformat()

    def record_error(self, error_key: str, max_tracked: int = 50) -> None:
        """Track a recurring error pattern, capped at ``max_tracked`` keys.

        When the cap is exceeded the least-frequent entry is dropped so that
        high-signal errors are always retained.
        """
        self.common_errors[error_key] = self.common_errors.get(error_key, 0) + 1
        # Prune least common if over limit
        if len(self.common_errors) > max_tracked:
            sorted_errors = sorted(
                self.common_errors.items(), key=lambda x: x[1], reverse=True,
            )
            self.common_errors = dict(sorted_errors[:max_tracked])

    def add_project_root(self, root: str) -> None:
        """Record a project root directory, deduplicating and capping at 100.

        Duplicate entries are ignored.  When the list would exceed 100 entries
        the oldest entry is dropped (FIFO).
        """
        if not root or root in self.project_roots_seen:
            return
        self.project_roots_seen.append(root)
        if len(self.project_roots_seen) > 100:
            self.project_roots_seen = self.project_roots_seen[-100:]

    def record_project(self, project_root: str, max_tracked: int = 100) -> None:
        """Track a project directory seen by KAIROS.

        Deprecated in favour of :meth:`add_project_root`.  The ``max_tracked``
        parameter is kept for backward compatibility but capped internally at
        100 to prevent unbounded growth.
        """
        self.add_project_root(project_root)

    def record_log_entry(self) -> None:
        """Record a daily log entry, updating the date stamp and total count.

        ``last_log_date`` is stored as ``YYYY-MM-DD`` (date-only) so that
        entries can be grouped by day without timestamp noise.
        """
        self.total_log_entries += 1
        self.last_log_date = datetime.now(UTC).strftime("%Y-%m-%d")

    def can_dream(self, min_hours: int = 24, min_sessions: int = 5) -> bool:
        """Check if enough time and sessions have passed for a dream run."""
        if not self.last_dream_timestamp:
            return self.total_sessions >= min_sessions

        try:
            last = datetime.fromisoformat(self.last_dream_timestamp)
            elapsed = (datetime.now(UTC) - last).total_seconds() / 3600
            sessions_since = self.total_sessions - (
                self.dream_count * min_sessions  # rough approximation
            )
            return elapsed >= min_hours and sessions_since >= min_sessions
        except (ValueError, TypeError):
            return True
