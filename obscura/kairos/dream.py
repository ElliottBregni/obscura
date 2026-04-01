"""
obscura.kairos.dream — Memory consolidation during idle ("dreaming").

Runs as a background process when KAIROS detects sufficient idle time.
Performs a 4-phase consolidation:

  1. **Orient** — Survey existing memory structure
  2. **Gather** — Collect new signal from daily logs and sessions
  3. **Consolidate** — Merge observations, resolve contradictions,
     convert vague insights to absolute facts
  4. **Prune** — Remove stale entries, keep MEMORY.md under limits

Gating order (cheapest first):
  - Time gate: minimum hours since last consolidation (default 24h)
  - Session gate: minimum sessions since last consolidation (default 5)
  - Lock gate: mutual exclusion via lock file
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_MEMORY_DIR = Path.home() / ".obscura" / "memory"
_LOCK_FILE = _MEMORY_DIR / ".consolidate-lock"
_MEMORY_INDEX = _MEMORY_DIR / "MEMORY.md"

# Limits matching claude-code.
MEMORY_INDEX_MAX_LINES = 200
MEMORY_INDEX_MAX_BYTES = 25_000

CONSOLIDATION_PROMPT = """\
# Dream: Memory Consolidation

You are performing memory consolidation for the KAIROS daemon.
Review recent observations and existing memories, then update
the memory files to reflect current truth.

## Phase 1 — Orient
- List the memory directory contents
- Read MEMORY.md (the index file)
- Skim existing topic files to understand current state

## Phase 2 — Gather
Look for new information worth persisting:
1. **Daily logs** (logs/YYYY/MM/YYYY-MM-DD.md) — the append-only stream
2. **Existing memories that drifted** — facts contradicted by recent evidence
3. **Session transcripts** — grep for specific context if needed

## Phase 3 — Consolidate
- Merge new signal into existing topic files
- Convert relative dates to absolute (e.g., "yesterday" → "2026-03-31")
- Delete facts that are now contradicted
- Create new topic files for genuinely new subjects
- Each memory file uses frontmatter: name, description, type (user/feedback/project/reference)

## Phase 4 — Prune and Index
- Keep MEMORY.md under 200 lines and 25KB
- Each entry: `[Title](file.md) — one-line description`
- Remove pointers to deleted files
- Remove stale or redundant entries

Rules:
- Never fabricate information — only persist what's in the logs/transcripts
- Prefer updating existing files over creating new ones
- Keep descriptions specific enough to judge relevance in future sessions
"""


class DreamConsolidator:
    """Memory consolidation engine for KAIROS dreaming.

    Usage::

        consolidator = DreamConsolidator()
        if consolidator.should_run():
            await consolidator.run()
    """

    def __init__(
        self,
        *,
        min_hours: float = 24.0,
        min_sessions: int = 5,
    ) -> None:
        self._min_hours = min_hours
        self._min_sessions = min_sessions

    def should_run(self) -> bool:
        """Check all gates to determine if consolidation should run."""
        # Gate 1: Time since last consolidation.
        last_at = self._last_consolidated_at()
        if last_at > 0:
            hours_elapsed = (time.time() - last_at) / 3600
            if hours_elapsed < self._min_hours:
                logger.debug("Dream skipped: %.1fh < %.1fh minimum", hours_elapsed, self._min_hours)
                return False

        # Gate 2: Session count since last consolidation.
        session_count = self._sessions_since(last_at)
        if session_count < self._min_sessions:
            logger.debug("Dream skipped: %d < %d sessions", session_count, self._min_sessions)
            return False

        # Gate 3: Lock availability.
        if self._is_locked():
            logger.debug("Dream skipped: lock held by another process")
            return False

        return True

    async def run(self) -> bool:
        """Execute the 4-phase dream consolidation.

        Returns True if consolidation completed successfully.
        """
        if not self._acquire_lock():
            return False

        try:
            logger.info("Dream consolidation starting...")
            _MEMORY_DIR.mkdir(parents=True, exist_ok=True)

            # Ensure MEMORY.md exists.
            if not _MEMORY_INDEX.exists():
                _MEMORY_INDEX.write_text(
                    "# Memory Index\n\nNo memories recorded yet.\n",
                    encoding="utf-8",
                )

            # Log the consolidation event.
            from obscura.kairos.daily_log import DailyLog
            DailyLog().append("Dream consolidation executed", source="dream")

            # Phase 1-4: In a full implementation, this would spawn
            # a forked agent with the CONSOLIDATION_PROMPT and limited
            # tool permissions (read-only + memory dir write).
            # For now, we do basic maintenance.
            self._prune_index()

            logger.info("Dream consolidation completed")
            return True

        except Exception:
            logger.warning("Dream consolidation failed", exc_info=True)
            self._rollback_lock()
            return False

        finally:
            self._update_lock_timestamp()

    def _last_consolidated_at(self) -> float:
        """Return timestamp of last consolidation (lock file mtime)."""
        if _LOCK_FILE.exists():
            return _LOCK_FILE.stat().st_mtime
        return 0.0

    def _sessions_since(self, since_ts: float) -> int:
        """Count event store sessions modified since timestamp."""
        events_db = Path.home() / ".obscura" / "events.db"
        if not events_db.exists():
            return 0
        try:
            import sqlite3
            conn = sqlite3.connect(str(events_db))
            cursor = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE created_at > ?",
                (since_ts,),
            )
            count = cursor.fetchone()[0]
            conn.close()
            return count
        except Exception:
            return 0

    def _is_locked(self) -> bool:
        """Check if another process holds the consolidation lock."""
        if not _LOCK_FILE.exists():
            return False
        try:
            content = _LOCK_FILE.read_text().strip()
            pid = int(content)
            # Check if PID is still running.
            os.kill(pid, 0)
            return True  # Process exists.
        except (ValueError, ProcessLookupError, PermissionError):
            return False  # Stale lock.

    def _acquire_lock(self) -> bool:
        """Try to acquire the consolidation lock."""
        _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        if self._is_locked():
            return False
        _LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True

    def _rollback_lock(self) -> None:
        """Remove lock on failure (allow retry)."""
        _LOCK_FILE.unlink(missing_ok=True)

    def _update_lock_timestamp(self) -> None:
        """Touch the lock file to record consolidation time."""
        _LOCK_FILE.touch()

    def _prune_index(self) -> None:
        """Ensure MEMORY.md stays within limits."""
        if not _MEMORY_INDEX.exists():
            return
        content = _MEMORY_INDEX.read_text(encoding="utf-8")
        lines = content.splitlines()
        if len(lines) > MEMORY_INDEX_MAX_LINES:
            lines = lines[:MEMORY_INDEX_MAX_LINES]
            lines.append("\n<!-- Truncated: index exceeded 200 lines -->")
            _MEMORY_INDEX.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if len(content.encode("utf-8")) > MEMORY_INDEX_MAX_BYTES:
            # Binary chop to fit.
            while len("\n".join(lines).encode("utf-8")) > MEMORY_INDEX_MAX_BYTES and lines:
                lines.pop()
            _MEMORY_INDEX.write_text("\n".join(lines) + "\n", encoding="utf-8")
