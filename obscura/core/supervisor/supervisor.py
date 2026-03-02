"""
obscura.core.supervisor.supervisor — The single-writer Supervisor coordinator.

Orchestrates the full lifecycle of a supervised run:

    acquire_lock → build_context → run_model ⇄ run_tools →
    commit_memory → finalize → release_lock

Every state change, tool execution, and memory commit is recorded
as an event for full replay. Hooks and heartbeats are first-class
session citizens.

Usage::

    from obscura.core.supervisor import Supervisor, SupervisorConfig

    supervisor = Supervisor(
        db_path="/tmp/supervisor.db",
        config=SupervisorConfig(),
    )

    async for event in supervisor.run(
        session_id="sess-1",
        prompt="Fix the auth bug",
        backend=backend,
        tool_registry=tool_registry,
    ):
        handle(event)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator

from obscura.core.supervisor.errors import (
    LockExpiredError,
    RunTimeoutError,
    SupervisorError,
)
from obscura.core.supervisor.heartbeat import SessionHeartbeatManager
from obscura.core.supervisor.lock import SessionLock
from obscura.core.supervisor.memory_gate import MemoryCommitGate
from obscura.core.supervisor.observability import RunObserver
from obscura.core.supervisor.prompt_assembler import PromptAssembler
from obscura.core.supervisor.schema import init_supervisor_schema
from obscura.core.supervisor.session_hooks import SessionHookManager
from obscura.core.supervisor.state_machine import SessionStateMachine
from obscura.core.supervisor.tool_snapshot import FrozenToolRegistry, ToolSnapshotStore
from obscura.core.supervisor.types import (
    MemoryCandidate,
    RunContext,
    SupervisorConfig,
    SupervisorEvent,
    SupervisorEventKind,
    SupervisorHookPoint,
    SupervisorState,
)

logger = logging.getLogger(__name__)


class Supervisor:
    """Single-writer coordinator for supervised agent runs.

    Serializes all writes to a session through a deterministic state
    machine backed by SQLite advisory locks. Ensures:

    - One active run per session
    - Frozen tool/prompt/memory snapshots per run
    - Append-only event log for replay
    - First-class hooks and heartbeats
    - Memory commit gating with deduplication
    - Drift detection
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        config: SupervisorConfig | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._config = config or SupervisorConfig()
        self._local = threading.local()
        self._lock = SessionLock(self._db_path, default_ttl=self._config.lock_ttl)
        self._tool_store = ToolSnapshotStore(self._db_path)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self) -> None:
        init_supervisor_schema(self._conn())

    # -----------------------------------------------------------------------
    # Main run entry point
    # -----------------------------------------------------------------------

    async def run(
        self,
        session_id: str,
        prompt: str,
        *,
        backend: Any = None,
        tool_registry: Any = None,
        agent_id: str | None = None,
        policy_id: str | None = None,
        memory_items: list[MemoryCandidate] | None = None,
        system_prompt: str = "",
        context_instructions: str = "",
        session_history: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[SupervisorEvent]:
        """Execute a supervised run.

        This is the main entry point. It:
        1. Acquires the session lock
        2. Builds context (freeze tools, assemble prompt, retrieve memory)
        3. Delegates to the agent loop for model/tool execution
        4. Gates and commits memory
        5. Finalizes and releases the lock

        Yields:
            SupervisorEvent for each state change, hook fire, heartbeat, etc.
        """
        run_id = str(uuid.uuid4())
        holder_id = str(uuid.uuid4())
        config = self._config

        # State machine
        sm = SessionStateMachine(session_id=session_id, run_id=run_id)

        # Observer
        observer = RunObserver(run_id=run_id, session_id=session_id)
        observer.start()

        # Heartbeat manager
        heartbeat = SessionHeartbeatManager(
            db_path=self._db_path,
            session_id=session_id,
            run_id=run_id,
            interval=config.heartbeat_interval,
        )

        # Hook manager
        hooks = SessionHookManager(
            db_path=self._db_path,
            session_id=session_id,
            run_id=run_id,
        )
        hooks.load_from_db()

        # Memory gate
        memory_gate = MemoryCommitGate(
            db_path=self._db_path,
            session_id=session_id,
            run_id=run_id,
            min_importance=config.memory_min_importance,
            max_batch_size=config.memory_commit_batch_size,
        )

        # Run context (populated during BUILD_CONTEXT)
        run_context: RunContext | None = None

        try:
            # ==============================================================
            # 1. ACQUIRE LOCK
            # ==============================================================
            lock_start = time.monotonic()
            lock_info = await self._lock.acquire(
                session_id,
                holder_id,
                timeout=config.lock_timeout,
                ttl=config.lock_ttl,
            )
            lock_wait_ms = (time.monotonic() - lock_start) * 1000
            observer.record_lock_acquired(lock_wait_ms)

            yield SupervisorEvent(
                kind=SupervisorEventKind.LOCK_ACQUIRED,
                run_id=run_id,
                session_id=session_id,
                payload={
                    "holder_id": holder_id,
                    "wait_ms": round(lock_wait_ms, 1),
                },
            )

            # Start heartbeat (refreshes lock TTL)
            heartbeat.on_tick(
                lambda hb: self._lock.heartbeat(
                    session_id, holder_id, ttl=config.lock_ttl
                )
            )
            await heartbeat.start()

            # Create run record
            await asyncio.to_thread(
                self._create_run_sync,
                run_id=run_id,
                session_id=session_id,
                agent_id=agent_id,
                policy_id=policy_id,
            )

            yield SupervisorEvent(
                kind=SupervisorEventKind.RUN_STARTED,
                run_id=run_id,
                session_id=session_id,
                payload={"agent_id": agent_id, "policy_id": policy_id},
            )

            # ==============================================================
            # 2. BUILD CONTEXT
            # ==============================================================
            transition_event = sm.transition(SupervisorState.BUILDING_CONTEXT)
            yield transition_event

            heartbeat.update_state(SupervisorState.BUILDING_CONTEXT)

            # Fire pre-build hooks
            await hooks.fire_before(
                SupervisorHookPoint.PRE_BUILD_CONTEXT,
                {"prompt": prompt, "session_id": session_id},
            )

            # Freeze tool registry
            tool_snapshot: FrozenToolRegistry | None = None
            if tool_registry is not None:
                specs = tool_registry.all() if hasattr(tool_registry, "all") else []
                tool_snapshot = FrozenToolRegistry.from_specs(specs)
                await asyncio.to_thread(
                    self._tool_store.save, tool_snapshot, run_id
                )
                yield SupervisorEvent(
                    kind=SupervisorEventKind.TOOLS_FROZEN,
                    run_id=run_id,
                    session_id=session_id,
                    payload={
                        "snapshot_id": tool_snapshot.snapshot_id,
                        "tool_count": tool_snapshot.tool_count,
                        "hash": tool_snapshot.hash[:12],
                    },
                )

            # Queue memory items for commit gating
            if memory_items:
                for item in memory_items:
                    memory_gate.queue(item)

            # Assemble prompt
            assembler = PromptAssembler(
                token_budget=config.prompt_token_budget,
                reserved_output_tokens=config.reserved_output_tokens,
                store_full_prompt=True,
            )
            if system_prompt:
                assembler.set_section("system_prompt", system_prompt)
            if context_instructions:
                assembler.set_section("context_instructions", context_instructions)
            if tool_snapshot:
                tool_defs = "\n".join(
                    f"- {t.name}: {t.description}" for t in tool_snapshot.tools
                )
                if tool_defs:
                    assembler.set_section("tool_definitions", tool_defs)
            if session_history:
                assembler.set_section("session_history", session_history)
            assembler.set_section("user_prompt", prompt)

            prompt_snapshot = assembler.freeze()

            yield SupervisorEvent(
                kind=SupervisorEventKind.PROMPT_ASSEMBLED,
                run_id=run_id,
                session_id=session_id,
                payload={
                    "prompt_hash": prompt_snapshot.prompt_hash[:12],
                    "total_tokens": prompt_snapshot.total_tokens,
                    "section_count": len(prompt_snapshot.sections),
                },
            )

            # Store prompt snapshot
            await asyncio.to_thread(
                self._store_prompt_snapshot_sync,
                run_id=run_id,
                prompt_snapshot=prompt_snapshot,
            )

            # Build run context (frozen)
            run_context = RunContext(
                run_id=run_id,
                session_id=session_id,
                prompt_hash=prompt_snapshot.prompt_hash,
                tool_snapshot_hash=tool_snapshot.hash if tool_snapshot else "",
                tool_names=tool_snapshot.tool_names if tool_snapshot else (),
                metadata=metadata or {},
            )

            # Record in observer
            observer.record_context(
                prompt_hash=run_context.prompt_hash,
                tool_snapshot_hash=run_context.tool_snapshot_hash,
                tool_count=tool_snapshot.tool_count if tool_snapshot else 0,
            )

            # Update run record
            await asyncio.to_thread(
                self._update_run_sync,
                run_id=run_id,
                prompt_hash=run_context.prompt_hash,
                tool_snapshot_id=tool_snapshot.snapshot_id if tool_snapshot else None,
                tool_registry_hash=run_context.tool_snapshot_hash,
            )

            yield SupervisorEvent(
                kind=SupervisorEventKind.CONTEXT_BUILT,
                run_id=run_id,
                session_id=session_id,
                payload={
                    "prompt_hash": run_context.prompt_hash[:12],
                    "tool_hash": run_context.tool_snapshot_hash[:12],
                    "tool_count": len(run_context.tool_names),
                },
            )

            # Fire post-build hooks
            await hooks.fire_after(
                SupervisorHookPoint.POST_BUILD_CONTEXT,
                {
                    "run_context": {
                        "run_id": run_id,
                        "prompt_hash": run_context.prompt_hash[:12],
                        "tool_hash": run_context.tool_snapshot_hash[:12],
                    }
                },
            )

            # ==============================================================
            # 3. MODEL TURN (transition to RUNNING_MODEL)
            # ==============================================================
            transition_event = sm.transition(SupervisorState.RUNNING_MODEL)
            yield transition_event

            heartbeat.update_state(SupervisorState.RUNNING_MODEL)

            yield SupervisorEvent(
                kind=SupervisorEventKind.MODEL_TURN_START,
                run_id=run_id,
                session_id=session_id,
                payload={"turn": 1},
            )

            # NOTE: Actual model execution is delegated to AgentLoop.
            # The Supervisor wraps AgentLoop, intercepting events for
            # state machine management, hook firing, and observability.
            #
            # In a full integration, the supervisor would:
            #   async for agent_event in agent_loop.run(prompt):
            #       yield supervisor_event(agent_event)
            #
            # For now, we yield the model turn events for the caller
            # to integrate with their existing AgentLoop.

            yield SupervisorEvent(
                kind=SupervisorEventKind.MODEL_TURN_END,
                run_id=run_id,
                session_id=session_id,
                payload={"turn": 1},
            )
            observer.observe(
                SupervisorEvent(kind=SupervisorEventKind.MODEL_TURN_START)
            )

            # ==============================================================
            # 4. COMMIT MEMORY
            # ==============================================================
            transition_event = sm.transition(SupervisorState.COMMITTING_MEMORY)
            yield transition_event

            heartbeat.update_state(SupervisorState.COMMITTING_MEMORY)

            # Fire pre-commit hooks
            await hooks.fire_before(
                SupervisorHookPoint.PRE_MEMORY_COMMIT,
                {"queue_size": memory_gate.queue_size},
            )

            if memory_gate.queue_size > 0:
                commit_result = await asyncio.to_thread(memory_gate.commit_sync)

                # Feed memory events to observer
                for evt in memory_gate.events:
                    observer.observe(evt)
                    yield evt

                yield SupervisorEvent(
                    kind=SupervisorEventKind.MEMORY_COMMIT,
                    run_id=run_id,
                    session_id=session_id,
                    payload={
                        "committed": commit_result.committed,
                        "deduplicated": commit_result.deduplicated,
                        "gated": commit_result.gated,
                        "errors": commit_result.errors,
                    },
                )

            await hooks.fire_after(
                SupervisorHookPoint.POST_MEMORY_COMMIT,
                {"queue_size": memory_gate.queue_size},
            )

            # ==============================================================
            # 5. FINALIZE
            # ==============================================================
            transition_event = sm.transition(SupervisorState.FINALIZING)
            yield transition_event

            heartbeat.update_state(SupervisorState.FINALIZING)

            # Fire pre-finalize hooks
            await hooks.fire_before(
                SupervisorHookPoint.PRE_FINALIZE,
                {"run_id": run_id},
            )

            # Stop heartbeat
            await heartbeat.stop()

            # Feed heartbeat events to observer
            for evt in heartbeat.events:
                observer.observe(evt)

            # Update run record
            await asyncio.to_thread(
                self._complete_run_sync,
                run_id=run_id,
                turn_count=observer.metrics.turn_count,
            )

            # Persist supervisor events
            all_events: list[SupervisorEvent] = []
            all_events.extend(sm.history)
            all_events.extend(hooks.events)
            all_events.extend(heartbeat.events)
            all_events.extend(memory_gate.events)

            await asyncio.to_thread(
                self._persist_events_sync, run_id, all_events
            )

            # Reset state machine
            sm.reset()

            # Release lock
            await self._lock.release(session_id, holder_id)
            observer.record_lock_released()

            yield SupervisorEvent(
                kind=SupervisorEventKind.LOCK_RELEASED,
                run_id=run_id,
                session_id=session_id,
                payload={"holder_id": holder_id},
            )

            # Fire post-finalize hooks
            await hooks.fire_after(
                SupervisorHookPoint.POST_FINALIZE,
                {"run_id": run_id},
            )

            # Finalize observer
            metrics = observer.finalize()

            yield SupervisorEvent(
                kind=SupervisorEventKind.RUN_COMPLETED,
                run_id=run_id,
                session_id=session_id,
                payload=metrics.to_dict(),
            )

        except SupervisorError as e:
            # Known supervisor error — record and emit
            logger.error("Supervisor error: %s", e)

            if sm.state != SupervisorState.IDLE:
                sm.fail(str(e))

            await asyncio.to_thread(
                self._fail_run_sync, run_id, str(e)
            )

            await heartbeat.stop()
            await self._lock.release(session_id, holder_id)

            yield SupervisorEvent(
                kind=SupervisorEventKind.RUN_FAILED,
                run_id=run_id,
                session_id=session_id,
                payload={
                    "error": str(e),
                    "category": e.category.value,
                    "retryable": e.retryable,
                },
            )

        except Exception as e:
            # Unexpected error — record and re-raise
            logger.exception("Unexpected supervisor error: %s", e)

            if sm.state != SupervisorState.IDLE:
                sm.fail(str(e))

            await asyncio.to_thread(
                self._fail_run_sync, run_id, str(e)
            )

            await heartbeat.stop()
            await self._lock.release(session_id, holder_id)

            yield SupervisorEvent(
                kind=SupervisorEventKind.RUN_FAILED,
                run_id=run_id,
                session_id=session_id,
                payload={"error": str(e), "category": "unknown"},
            )

        finally:
            # Ensure cleanup
            heartbeat.close()
            hooks.close()
            memory_gate.close()

    # -----------------------------------------------------------------------
    # Replay
    # -----------------------------------------------------------------------

    async def replay_run(self, run_id: str) -> dict[str, Any]:
        """Reconstruct the full context of a historical run.

        Returns a dict with:
        - agent_id, agent_version_hash
        - policy_id, policy_hash
        - tool_snapshot (names + hashes)
        - prompt_hash
        - memory_item_ids
        - event_count
        - all events
        """
        run = await asyncio.to_thread(self._get_run_sync, run_id)
        if run is None:
            raise ValueError(f"Run not found: {run_id}")

        events = await asyncio.to_thread(self._get_events_sync, run_id)

        # Load tool snapshot
        tool_snapshot = await asyncio.to_thread(
            self._tool_store.load_for_run, run_id
        )

        result: dict[str, Any] = {
            "run_id": run["run_id"],
            "session_id": run["session_id"],
            "agent_id": run.get("agent_id"),
            "policy_id": run.get("policy_id"),
            "state": run["state"],
            "prompt_hash": run.get("prompt_hash"),
            "tool_registry_hash": run.get("tool_registry_hash"),
            "memory_item_ids": json.loads(run.get("memory_item_ids", "[]")),
            "turn_count": run.get("turn_count", 0),
            "started_at": run.get("started_at"),
            "completed_at": run.get("completed_at"),
            "error": run.get("error"),
            "event_count": len(events),
            "events": events,
        }

        if tool_snapshot:
            result["tool_snapshot"] = {
                "snapshot_id": tool_snapshot.snapshot_id,
                "hash": tool_snapshot.hash,
                "tool_names": list(tool_snapshot.tool_names),
                "tool_count": tool_snapshot.tool_count,
            }

        return result

    # -----------------------------------------------------------------------
    # Recovery
    # -----------------------------------------------------------------------

    async def recover(self) -> dict[str, int]:
        """Recover from crashes.

        Cleans up:
        - Expired locks
        - Orphaned runs (started but never completed)

        Returns counts of recovered items.
        """
        expired_locks = await self._lock.cleanup_expired()
        orphaned_runs = await asyncio.to_thread(self._recover_orphaned_runs_sync)

        if expired_locks or orphaned_runs:
            logger.info(
                "Recovery: %d expired locks, %d orphaned runs",
                expired_locks,
                orphaned_runs,
            )

        return {"expired_locks": expired_locks, "orphaned_runs": orphaned_runs}

    # -----------------------------------------------------------------------
    # Sync DB helpers (run in asyncio.to_thread)
    # -----------------------------------------------------------------------

    def _create_run_sync(
        self,
        run_id: str,
        session_id: str,
        agent_id: str | None = None,
        policy_id: str | None = None,
    ) -> None:
        conn = self._conn()
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO supervisor_runs "
            "(run_id, session_id, agent_id, policy_id, state, started_at, metadata) "
            "VALUES (?, ?, ?, ?, 'building_context', ?, '{}')",
            (run_id, session_id, agent_id, policy_id, now),
        )
        conn.commit()

    def _update_run_sync(
        self,
        run_id: str,
        *,
        prompt_hash: str | None = None,
        tool_snapshot_id: str | None = None,
        tool_registry_hash: str | None = None,
        state: str | None = None,
    ) -> None:
        conn = self._conn()
        sets: list[str] = []
        params: list[Any] = []

        if prompt_hash is not None:
            sets.append("prompt_hash = ?")
            params.append(prompt_hash)
        if tool_snapshot_id is not None:
            sets.append("tool_snapshot_id = ?")
            params.append(tool_snapshot_id)
        if tool_registry_hash is not None:
            sets.append("tool_registry_hash = ?")
            params.append(tool_registry_hash)
        if state is not None:
            sets.append("state = ?")
            params.append(state)

        if not sets:
            return

        params.append(run_id)
        conn.execute(
            f"UPDATE supervisor_runs SET {', '.join(sets)} WHERE run_id = ?",
            params,
        )
        conn.commit()

    def _complete_run_sync(self, run_id: str, turn_count: int = 0) -> None:
        conn = self._conn()
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE supervisor_runs SET state = 'completed', "
            "completed_at = ?, turn_count = ? WHERE run_id = ?",
            (now, turn_count, run_id),
        )
        conn.commit()

    def _fail_run_sync(self, run_id: str, error: str) -> None:
        conn = self._conn()
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE supervisor_runs SET state = 'failed', "
            "completed_at = ?, error = ? WHERE run_id = ?",
            (now, error, run_id),
        )
        conn.commit()

    def _get_run_sync(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM supervisor_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def _get_events_sync(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM supervisor_events WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _persist_events_sync(
        self,
        run_id: str,
        events: list[SupervisorEvent],
    ) -> None:
        conn = self._conn()
        for seq, event in enumerate(events, start=1):
            conn.execute(
                "INSERT OR IGNORE INTO supervisor_events "
                "(run_id, seq, kind, payload, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    seq,
                    event.kind.value,
                    json.dumps(event.payload, default=str),
                    event.timestamp.isoformat(),
                ),
            )
        conn.commit()

    def _store_prompt_snapshot_sync(
        self,
        run_id: str,
        prompt_snapshot: Any,
    ) -> None:
        conn = self._conn()
        snapshot_id = str(uuid.uuid4())
        sections_json = json.dumps(
            [
                {"name": s.name, "content": s.content, "tokens": s.token_estimate}
                for s in prompt_snapshot.sections
            ],
            default=str,
        )
        conn.execute(
            "INSERT OR REPLACE INTO prompt_snapshots "
            "(snapshot_id, run_id, prompt_hash, sections_json, token_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                run_id,
                prompt_snapshot.prompt_hash,
                sections_json,
                prompt_snapshot.total_tokens,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()

    def _recover_orphaned_runs_sync(self) -> int:
        conn = self._conn()
        now = datetime.now(UTC).isoformat()
        cursor = conn.execute(
            "UPDATE supervisor_runs SET state = 'failed', "
            "completed_at = ?, error = 'crash_recovery' "
            "WHERE state NOT IN ('completed', 'failed', 'idle') "
            "AND completed_at IS NULL",
            (now,),
        )
        conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        """Close all connections."""
        self._lock.close()
        self._tool_store.close()
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None
