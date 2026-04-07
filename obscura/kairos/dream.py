"""obscura.kairos.dream — Memory consolidation during idle ("dreaming").

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

import contextlib
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Tools the dream consolidation agent is allowed to use.
_DREAM_AGENT_TOOLS: list[str] = [
    "read_text_file",
    "write_text_file",
    "edit_text_file",
    "append_text_file",
    "list_directory",
    "find_files",
    "grep_files",
    "goal",
    "profile_get",
    "profile_update",
    "profile_recall",
    "profile_set",
    "profile_forget",
    "profile_sync",
]

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

## Phase 0 — User Profile Update
Before anything else, scan today's daily log and any available session context for
new facts about the user. For each new fact found:
1. Categorize it: preference (working style, tool choices, likes/hates),
   fact (career info, background, skills — 90-day half-life), or
   episode (current project context, recent events — 7-day half-life)
2. Call profile_update(fact=<text>, memory_type=<category>) for each new finding
3. Call profile_get() after to confirm the profile is up to date

Examples of profile-worthy facts:
- User mentioned switching to a new framework → fact
- User said they hate confirmation prompts → preference
- User is currently debugging auth middleware → episode
- User got promoted / changed role → fact

Do NOT fabricate. Only persist things explicitly observed in the logs.

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

## Phase 5 — Task Carry-forward
Scan daily logs and session transcripts for incomplete work items:
1. Look for `todo_write` tool calls or TodoWrite calls that contain tasks in
   `in_progress` or `pending` state at session end
2. Look for explicit statements like "next step is...", "still need to...",
   "TODO:", "FIXME:", "need to implement..."
3. Look for partial work — files being edited, tests that were failing,
   features in mid-implementation

Write results to `pending_tasks.md` in the memory directory:
- Format each task as: `- [ ] <task description> (from: <source date/session>)`
- Include enough context to resume the task cold (file names, function names, ticket IDs)
- Mark previously-completed tasks with `[x]` and keep for 7 days, then remove
- Keep the file under 50 lines; drop oldest completed tasks first

## Phase 6 — Goal Progress Review
Read all goal files in ~/.obscura/goals/ (use goal_list, then goal_get for each):
1. For each active/in_progress goal, scan today's daily log for related work
2. Update the goal's `progress` percentage based on evidence (use goal_update)
3. If all acceptance criteria appear met, mark the goal completed (goal_complete)
4. If no progress in 7+ days, add a note to the goal suggesting review or abandonment
5. Do NOT create or delete goals — only update existing ones

## Phase 7 — User Profile Vector Consolidation
Maintain the vector-backed user profile with per-category decay:
1. Read current profile with scores: profile_get(include_scores=true)
2. Scan recent daily logs for profile-relevant information:
   - Career changes, new skills, role updates → category "career" or "skill"
   - Stated preferences, working style observations → category "preference"
   - Personal facts (location, interests, habits) → category "personal"
   - Ephemeral observations → category "learned"
3. For new structured facts: use profile_set(key=<dotted.key>, value=<text>, category=<cat>)
   - Keys should be descriptive: "career.target_company", "personal.location", "skill.primary_language"
4. For contradicted facts: profile_forget the old key, then profile_set the correction
5. If user_profile.md exists and vector profile has < 10 facts, run profile_sync to migrate
6. Do NOT delete identity facts (name, email) — they are immune to decay

## Phase 8 — Vault Sync
Sync the Obsidian vault at ~/.obscura/vault/ with Obscura state:
1. Scan `vault/user/` for files with frontmatter meta tags:
   - `type: goal` → create/update goals on the GoalBoard
   - `type: profile` → append facts to user profile
   - `type: task`, `type: reference`, `type: note` → ingest into vector memory
2. Export current Obscura goals to `vault/agent/goals/` as markdown files
3. Export a profile summary to `vault/agent/profile-summary.md`
4. Check `vault/shared/` for conflicts — if both user and agent edited the same
   file since last sync, fork into `.user.md` and `.agent.md` variants for
   manual merge
5. Respect ownership zones:
   - NEVER write to `vault/user/` — read only
   - Write freely to `vault/agent/`
   - Use `write_agent_shared()` for `vault/shared/` to enable fork-merge

The vault sync runs automatically via VaultSync.sync(). Just call it once.

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

    def _memory_dir(self) -> Path:
        """Runtime-resolved memory directory (respects $HOME)."""
        return Path.home() / ".obscura" / "memory"

    def _lock_file(self) -> Path:
        """Path to the consolidation lock file."""
        return self._memory_dir() / ".consolidate-lock"

    def _memory_index(self) -> Path:
        """Path to the MEMORY.md index file."""
        return self._memory_dir() / "MEMORY.md"

    def _pending_tasks_file(self) -> Path:
        """Path to the pending_tasks.md carry-forward file."""
        return self._memory_dir() / "pending_tasks.md"

    def should_run(self) -> bool:
        """Check all gates to determine if consolidation should run."""
        # Gate 1: Time since last consolidation.
        last_at = self._last_consolidated_at()
        if last_at > 0:
            hours_elapsed = (time.time() - last_at) / 3600
            if hours_elapsed < self._min_hours:
                logger.debug(
                    "Dream skipped: %.1fh < %.1fh minimum",
                    hours_elapsed,
                    self._min_hours,
                )
                return False

        # Gate 2: Session count since last consolidation.
        session_count = self._sessions_since(last_at)
        if session_count < self._min_sessions:
            logger.debug(
                "Dream skipped: %d < %d sessions",
                session_count,
                self._min_sessions,
            )
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
            self._memory_dir().mkdir(parents=True, exist_ok=True)

            # Ensure MEMORY.md exists.
            if not self._memory_index().exists():
                self._memory_index().write_text(
                    "# Memory Index\n\nNo memories recorded yet.\n",
                    encoding="utf-8",
                )

            # Log the consolidation event.
            from obscura.kairos.daily_log import DailyLog

            DailyLog().append("Dream consolidation executed", source="dream")

            # Run vault sync before agent consolidation so the agent
            # sees the latest vault state in goals and memory.
            await self._run_vault_sync()

            # Phase 1-4: Spawn a forked agent with the CONSOLIDATION_PROMPT.
            # Uses the default backend (copilot) with read-only + memory dir
            # write permissions and a capped turn budget.
            agent_result = await self._run_consolidation_agent()

            # Always prune regardless of agent outcome.
            self._prune_index()

            if agent_result:
                logger.info("Dream consolidation completed (agent ran successfully)")
            else:
                logger.info(
                    "Dream consolidation completed (agent unavailable — pruned only)",
                )
            return True

        except Exception:
            logger.warning("Dream consolidation failed", exc_info=True)
            self._rollback_lock()
            return False

        finally:
            self._update_lock_timestamp()

    async def _run_vault_sync(self) -> None:
        """Run vault sync as part of dream consolidation."""
        try:
            from obscura.kairos.vault_sync import VaultSync

            vault = VaultSync()
            if not vault.vault_dir.is_dir():
                logger.debug("Vault directory does not exist, skipping vault sync")
                return

            report = await vault.sync()
            from obscura.kairos.daily_log import DailyLog

            DailyLog().append(report.summary(), source="vault")
            logger.info("Dream vault sync: %s", report.summary())
        except Exception:
            logger.warning("Dream vault sync failed", exc_info=True)

    async def _run_consolidation_agent(self) -> bool:
        """Spawn a forked ObscuraClient agent to run the 4-phase consolidation.

        Uses the default backend (copilot) with a 15-turn budget.
        The agent is given the CONSOLIDATION_PROMPT as its system prompt and
        instructed to read/write only under ~/.obscura/memory/.

        Returns True if the agent completed without exception.
        """
        try:
            from obscura.core.client import ObscuraClient
            from obscura.core.config import ObscuraConfig

            # Gather the tools the dream agent needs.
            dream_tools = []
            try:
                from obscura.tools.system import get_system_tool_specs

                dream_tools.extend(get_system_tool_specs())
            except Exception:
                pass
            try:
                from obscura.tools.goal_tools import get_goal_tool_specs

                dream_tools.extend(get_goal_tool_specs())
            except Exception:
                pass
            try:
                from obscura.tools.profile_tools import get_profile_tool_specs

                dream_tools.extend(get_profile_tool_specs())
            except Exception:
                pass

            cfg = ObscuraConfig.from_env()
            async with ObscuraClient(
                cfg.default_backend,
                model=None,
                system_prompt=CONSOLIDATION_PROMPT,
                tools=dream_tools,
            ) as client:
                result = await client.run_loop_to_completion(
                    "Begin memory consolidation. Follow all phases in the system prompt.",
                    max_turns=15,
                    tool_allowlist=_DREAM_AGENT_TOOLS,
                )
                logger.debug(
                    "Dream agent output (%d chars): %s...",
                    len(result),
                    result[:200],
                )
                return True
        except Exception:
            logger.warning("Dream consolidation agent failed", exc_info=True)
            return False

    def _last_consolidated_at(self) -> float:
        """Return timestamp of last consolidation.

        Prefer explicit timestamp stored in the lock file metadata (JSON).
        Fall back to the lock file mtime for compatibility.
        """
        if not self._lock_file().exists():
            return 0.0
        try:
            raw = self._lock_file().read_text(encoding="utf-8")
            import json

            data = json.loads(raw)
            ts = float(data.get("ts", 0))
            if ts > 0:
                return ts
        except Exception:
            # Fall back to mtime if file doesn't contain JSON or parse fails.
            try:
                return self._lock_file().stat().st_mtime
            except Exception:
                return 0.0
        return 0.0

    def _sessions_since(self, since_ts: float) -> int:
        """Count event store sessions modified since timestamp."""
        try:
            from obscura.core.config import ObscuraConfig

            cfg = ObscuraConfig.from_env()
            events_db = (
                Path(cfg.data_dir) / "events.db"
                if hasattr(cfg, "data_dir")
                else Path.home() / ".obscura" / "events.db"
            )
        except Exception:
            events_db = Path.home() / ".obscura" / "events.db"
        if not events_db.exists():
            return 0
        try:
            import sqlite3

            conn = sqlite3.connect(str(events_db))
            # Convert since_ts (seconds since epoch) to ISO8601 UTC string to
            # compare against created_at TEXT columns stored in ISO format.
            try:
                since_iso = datetime.fromtimestamp(float(since_ts), tz=UTC).isoformat()
            except Exception:
                since_iso = datetime.fromtimestamp(time.time(), tz=UTC).isoformat()
            cursor = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE created_at > ?",
                (since_iso,),
            )
            count = cursor.fetchone()[0]
            conn.close()
            return count
        except Exception:
            return 0

    def _is_locked(self) -> bool:
        """Check if another process holds the consolidation lock.

        Lock file now stores JSON: {"pid": <int>, "ts": <float epoch seconds>}.
        Returns True if the PID appears to be running. On PermissionError,
        conservatively treat the lock as held (True).
        """
        if not self._lock_file().exists():
            return False
        try:
            raw = self._lock_file().read_text(encoding="utf-8")
            import json

            data = json.loads(raw)
            pid = int(data.get("pid", 0))
        except Exception:
            # Backwards compatibility: file might contain plain PID.
            try:
                pid = int(self._lock_file().read_text(encoding="utf-8").strip())
            except Exception:
                return False

        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            # PID does not exist -> stale lock
            return False
        except PermissionError:
            # Unable to query PID -> assume it's held by another user/process
            return True
        except Exception:
            return False

    def _acquire_lock(self) -> bool:
        """Try to acquire the consolidation lock."""
        self._memory_dir().mkdir(parents=True, exist_ok=True)
        if self._is_locked():
            return False
        # Write JSON metadata atomically.
        try:
            import json

            meta = {"pid": os.getpid(), "ts": time.time()}
            tmp = self._lock_file().with_suffix(".tmp")
            tmp.write_text(json.dumps(meta), encoding="utf-8")
            os.replace(str(tmp), str(self._lock_file()))
            return True
        except Exception:
            return False

    def _rollback_lock(self) -> None:
        """Remove lock on failure (allow retry)."""
        self._lock_file().unlink(missing_ok=True)

    def _update_lock_timestamp(self) -> None:
        """Update the lock file timestamp stored in JSON metadata.

        Fall back to touching the file if parsing/writing fails.
        """
        if not self._lock_file().exists():
            return
        try:
            import json

            raw = self._lock_file().read_text(encoding="utf-8")
            data = json.loads(raw)
            data["ts"] = time.time()
            tmp = self._lock_file().with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(str(tmp), str(self._lock_file()))
        except Exception:
            with contextlib.suppress(Exception):
                self._lock_file().touch()

    def _prune_index(self) -> None:
        """Ensure MEMORY.md stays within limits."""
        if not self._memory_index().exists():
            return
        content = self._memory_index().read_text(encoding="utf-8")
        lines = content.splitlines()
        if len(lines) > MEMORY_INDEX_MAX_LINES:
            lines = lines[:MEMORY_INDEX_MAX_LINES]
            lines.append("\n<!-- Truncated: index exceeded 200 lines -->")
            self._memory_index().write_text("\n".join(lines) + "\n", encoding="utf-8")
        if len(content.encode("utf-8")) > MEMORY_INDEX_MAX_BYTES:
            # Binary chop to fit.
            while (
                len("\n".join(lines).encode("utf-8")) > MEMORY_INDEX_MAX_BYTES and lines
            ):
                lines.pop()
            self._memory_index().write_text("\n".join(lines) + "\n", encoding="utf-8")
