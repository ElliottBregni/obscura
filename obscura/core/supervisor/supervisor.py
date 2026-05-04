"""obscura.core.supervisor.supervisor — The single-writer Supervisor coordinator.

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
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from obscura.auth.cli_user import current_cli_user
from obscura.core.supervisor.db_backend import (
    DatabaseBackend,
    SQLiteSupervisorBackend,
    create_supervisor_backend,
    translate_sql,
)
from obscura.core.supervisor.errors import (
    LockExpiredError,
    SupervisorError,
)
from obscura.core.supervisor.heartbeat import SessionHeartbeatManager
from obscura.core.supervisor.lock import SessionLock
from obscura.core.supervisor.memory_gate import MemoryCommitGate
from obscura.core.supervisor.observability import RunObserver
from obscura.core.supervisor.prompt_assembler import PromptAssembler
from obscura.core.supervisor.session_hooks import SessionHookManager
from obscura.core.supervisor.state_machine import SessionStateMachine
from obscura.core.supervisor.tool_snapshot import FrozenToolRegistry, ToolSnapshotStore
from obscura.core.enums.lifecycle import SupervisorState
from obscura.core.supervisor.types import (
    MemoryCandidate,
    RunContext,
    SupervisorConfig,
    SupervisorEvent,
    SupervisorEventKind,
    SupervisorHookPoint,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from obscura.core.types import ToolSpec

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
        db_path: str | Path | None = None,
        *,
        config: SupervisorConfig | None = None,
        backend: DatabaseBackend | None = None,
    ) -> None:
        if backend is not None:
            self._backend = backend
        elif db_path is not None:
            self._backend = SQLiteSupervisorBackend(db_path)
        else:
            self._backend = create_supervisor_backend()
        self._config = config or SupervisorConfig()
        self._lock = SessionLock(
            backend=self._backend, default_ttl=self._config.lock_ttl
        )
        self._tool_store = ToolSnapshotStore(backend=self._backend)

    def _sql(self, sql: str) -> str:
        return translate_sql(sql, self._backend.dialect)

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
        metadata: Mapping[str, Any] | None = None,
        agent_loop: Any | None = None,
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
            session_id=session_id,
            run_id=run_id,
            interval=config.heartbeat_interval,
            backend=self._backend,
        )

        # Hook manager
        hooks = SessionHookManager(
            session_id=session_id,
            run_id=run_id,
            backend=self._backend,
        )
        hooks.load_from_db()

        # Best-effort: register Kairos pre-tool guard if available to enforce
        # safety for background-initiated tool calls (opt-in per-session).
        try:
            from obscura.kairos.pretool import register_pretool_guard
            from obscura.kairos.guards import pre_tool_use_guard

            try:
                register_pretool_guard(hooks, pre_tool_use_guard)
                logger.debug("Registered Kairos PRE_TOOL_USE guard on session hooks")
            except Exception as _reg_exc:
                logger.debug("Could not bind Kairos PRE_TOOL_USE guard: %s", _reg_exc)
        except Exception:
            # Kairos modules not available in this environment; continue silently.
            logger.debug("Kairos pre-tool guard not available; skipping")

        # Register eval hooks (turn-level + session gate)
        try:
            from obscura.core.supervisor.eval_hooks import (
                make_session_eval_gate,
                make_turn_eval_hook,
            )

            point, ref, handler = make_turn_eval_hook()
            hooks.register(point, "after", ref, handler, persist=False)
            point, ref, handler = make_session_eval_gate()
            hooks.register(point, "before", ref, handler, persist=False)
            logger.debug("Eval hooks registered (turn + session gate)")
        except Exception as exc:
            logger.debug("Could not register eval hooks: %s", exc)

        # Register Arbiter judge hooks (quality gating for agents).
        try:
            from obscura.arbiter.hooks import register_arbiter_hooks
            from obscura.arbiter.notify import set_hook_manager
            from obscura.arbiter.types import ArbiterConfig

            # Detect daemon/background context from metadata.
            _meta = metadata or {}
            _is_daemon = bool(
                _meta.get("agent_type") == "daemon"
                or _meta.get("is_daemon")
                or _meta.get("initiator") in ("daemon", "background", "kairos")
            )
            register_arbiter_hooks(
                hooks,
                config=ArbiterConfig(is_daemon=_is_daemon),
                session_id=session_id,
                run_id=run_id,
            )
            set_hook_manager(hooks)
            logger.debug("Arbiter hooks registered")
        except Exception as exc:
            logger.debug("Could not register Arbiter hooks: %s", exc)

        # Register vault sync hooks (ingest on build, export on finalize).
        try:
            from obscura.core.supervisor.vault_hook import register_vault_hooks

            register_vault_hooks(hooks)
            logger.debug("Vault hooks registered")
        except Exception as exc:
            logger.debug("Could not register vault hooks: %s", exc)

        # Register profile + goal context hooks
        try:
            from obscura.core.supervisor.profile_goal_hook import (
                register_profile_goal_hooks,
            )

            # Best-effort user resolution for profile injection.
            register_profile_goal_hooks(hooks, user=current_cli_user())
        except Exception as exc:
            logger.debug("Could not register profile/goal hooks: %s", exc)

        # Memory gate
        memory_gate = MemoryCommitGate(
            session_id=session_id,
            run_id=run_id,
            min_importance=config.memory_min_importance,
            max_batch_size=config.memory_commit_batch_size,
            backend=self._backend,
        )

        # Run context (populated during BUILD_CONTEXT)
        run_context: RunContext | None = None

        try:
            # ==============================================================
            # 1. ACQUIRE LOCK
            # ==============================================================
            lock_start = time.monotonic()
            await self._lock.acquire(
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

            # Start heartbeat (refreshes lock TTL).
            # FIX: Use a named async callback instead of a bare lambda so that
            # a lost/expired lock is detected and surfaced — not silently swallowed.
            # _heartbeat_sync returns False when the lock row is gone (stolen or
            # expired), which the raw lambda never checked.  The named callback
            # raises LockExpiredError on False so _tick() logs it and the run
            # task is cancelled cleanly rather than continuing with a dead lock.
            _run_task: asyncio.Task[Any] | None = None

            async def _heartbeat_tick(hb: Any) -> None:
                refreshed = await self._lock.heartbeat(
                    session_id,
                    holder_id,
                    ttl=config.lock_ttl,
                )
                if not refreshed:
                    logger.critical(
                        "Heartbeat lost lock for session %s run %s (seq=%d) "
                        "— lock was stolen or expired; cancelling run task",
                        session_id,
                        run_id,
                        hb.seq,
                    )
                    if _run_task is not None and not _run_task.done():
                        _run_task.cancel()
                    raise LockExpiredError(session_id, holder_id)

            heartbeat.on_tick(_heartbeat_tick)
            # FIX: Capture current task BEFORE starting the heartbeat so the

            # _heartbeat_tick closure always has a valid reference even if the

            # first tick fires immediately during await heartbeat.start().

            _run_task = asyncio.current_task()

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

            # Fire pre-build hooks (hooks may inject context via dict mutation)
            hook_context: dict[str, Any] = {"prompt": prompt, "session_id": session_id}
            await hooks.fire_before(
                SupervisorHookPoint.PRE_BUILD_CONTEXT,
                hook_context,
            )

            # Freeze tool registry
            tool_snapshot: FrozenToolRegistry | None = None
            if tool_registry is not None:
                specs: list[ToolSpec] = (
                    tool_registry.all() if hasattr(tool_registry, "all") else []
                )
                tool_snapshot = FrozenToolRegistry.from_specs(specs)
                await asyncio.to_thread(
                    self._tool_store.save,
                    tool_snapshot,
                    run_id,
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

            # Construct and attach tool router if backend supports it.
            # The Agent.start() path (agents.py) is the primary wiring point
            # since it has access to the CapabilityIndex.  This path serves
            # as a fallback for supervisor-only runs (no Agent wrapper).
            if (
                tool_snapshot is not None
                and backend is not None
                and hasattr(backend, "set_tool_router")
                and getattr(backend, "_tool_router", None) is None
            ):
                try:
                    from obscura.core.compiler.compiled import ToolRoutingConfig
                    from obscura.core.tool_router import ToolRouter
                    from obscura.core.tool_score_index import ToolScoreIndex

                    routing_config = ToolRoutingConfig()
                    score_index = ToolScoreIndex()
                    router = ToolRouter(
                        config=routing_config,
                        score_index=score_index,
                        backend=getattr(backend, "_backend_type", "copilot")
                        if hasattr(backend, "_backend_type")
                        else "copilot",
                    )
                    backend.set_tool_router(router)
                except Exception:
                    logger.debug(
                        "Could not attach tool router to backend",
                        exc_info=True,
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

            # Inject hook-provided context sections (goal board, profile, vector memory).
            for ctx_key, section_name in (
                ("_goal_context", "goal_context"),
                ("_profile_context", "profile_context"),
                ("_vector_memory_context", "vector_memory_context"),
                ("_vault_context", "vault_context"),
                ("_arbiter_context", "arbiter_context"),
            ):
                ctx_value = hook_context.get(ctx_key)
                if ctx_value:
                    assembler.set_section(section_name, ctx_value)

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
                    },
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

            # Fire pre-model hooks
            await hooks.fire_before(
                SupervisorHookPoint.PRE_MODEL_TURN,
                {"prompt": prompt, "run_id": run_id},
            )

            # Drive the agent loop if provided, consuming its events
            # and wrapping them as supervisor events for observability.
            turn_count = 0
            if agent_loop is not None:
                try:
                    async for agent_event in agent_loop.run(
                        prompt, session_id=session_id
                    ):
                        # Track turns for the observer
                        event_kind = getattr(agent_event, "kind", None)
                        if event_kind is not None:
                            kind_value = (
                                event_kind.value
                                if hasattr(event_kind, "value")
                                else str(event_kind)
                            )
                        else:
                            kind_value = "unknown"

                        # Count turn completions
                        from obscura.core.enums.agent import AgentEventKind as _AEK

                        if event_kind == _AEK.TURN_COMPLETE:
                            turn_count += 1
                        elif event_kind == _AEK.TOOL_CALL:
                            # Transition to RUNNING_TOOLS on tool calls
                            if sm.state == SupervisorState.RUNNING_MODEL:
                                try:
                                    t_evt = sm.transition(SupervisorState.RUNNING_TOOLS)
                                    yield t_evt
                                    heartbeat.update_state(
                                        SupervisorState.RUNNING_TOOLS
                                    )
                                except Exception:
                                    logger.debug(
                                        "suppressed exception in run", exc_info=True
                                    )
                        elif event_kind == _AEK.TURN_START:
                            # Transition back to RUNNING_MODEL on new turns
                            if sm.state == SupervisorState.RUNNING_TOOLS:
                                try:
                                    t_evt = sm.transition(SupervisorState.RUNNING_MODEL)
                                    yield t_evt
                                    heartbeat.update_state(
                                        SupervisorState.RUNNING_MODEL
                                    )
                                except Exception:
                                    logger.debug(
                                        "suppressed exception in run", exc_info=True
                                    )

                        # Wrap agent event as supervisor event
                        sv_event = SupervisorEvent(
                            kind=SupervisorEventKind.MODEL_TURN_END,
                            run_id=run_id,
                            session_id=session_id,
                            payload={
                                "agent_event_kind": kind_value,
                                "agent_event": agent_event,
                            },
                        )
                        observer.observe(sv_event)
                        yield sv_event
                except Exception as loop_exc:
                    logger.error("Agent loop error in supervisor: %s", loop_exc)
                    yield SupervisorEvent(
                        kind=SupervisorEventKind.RUN_FAILED,
                        run_id=run_id,
                        session_id=session_id,
                        payload={"error": str(loop_exc), "source": "agent_loop"},
                    )
            else:
                # Legacy path: no agent_loop provided — yield placeholder
                # for the caller to integrate with their own AgentLoop.
                pass

            # Ensure we're back in RUNNING_MODEL before transitioning
            if sm.state == SupervisorState.RUNNING_TOOLS:
                try:
                    t_evt = sm.transition(SupervisorState.RUNNING_MODEL)
                    yield t_evt
                except Exception:
                    logger.debug("suppressed exception in run", exc_info=True)

            # Fire post-model hooks
            await hooks.fire_after(
                SupervisorHookPoint.POST_MODEL_TURN,
                {"run_id": run_id},
            )

            _final_turn_count = turn_count if agent_loop is not None else 1  # noqa: F841
            _turn_end_event = SupervisorEvent(
                kind=SupervisorEventKind.MODEL_TURN_END,
                run_id=run_id,
                session_id=session_id,
                payload={"turn": _final_turn_count},
            )
            observer.observe(_turn_end_event)
            yield _turn_end_event

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
                self._persist_events_sync,
                run_id,
                all_events,
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
            logger.exception("Supervisor error: %s", e)

            if sm.state != SupervisorState.IDLE:
                sm.fail(str(e))

            await asyncio.to_thread(
                self._fail_run_sync,
                run_id,
                str(e),
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
                self._fail_run_sync,
                run_id,
                str(e),
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
            msg = f"Run not found: {run_id}"
            raise ValueError(msg)

        events = await asyncio.to_thread(self._get_events_sync, run_id)

        # Load tool snapshot
        tool_snapshot = await asyncio.to_thread(
            self._tool_store.load_for_run,
            run_id,
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
        conn = self._backend.get_conn()
        try:
            now = datetime.now(UTC).isoformat()
            conn.execute(
                self._sql(
                    "INSERT INTO supervisor_runs "
                    "(run_id, session_id, agent_id, policy_id, state, started_at, metadata) "
                    "VALUES (?, ?, ?, ?, 'building_context', ?, '{}')"
                ),
                (run_id, session_id, agent_id, policy_id, now),
            )
            conn.commit()
        finally:
            self._backend.put_conn(conn)

    def _update_run_sync(
        self,
        run_id: str,
        *,
        prompt_hash: str | None = None,
        tool_snapshot_id: str | None = None,
        tool_registry_hash: str | None = None,
        state: str | None = None,
    ) -> None:
        conn = self._backend.get_conn()
        try:
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
                self._sql(
                    f"UPDATE supervisor_runs SET {', '.join(sets)} WHERE run_id = ?"
                ),
                params,
            )
            conn.commit()
        finally:
            self._backend.put_conn(conn)

    def _complete_run_sync(self, run_id: str, turn_count: int = 0) -> None:
        conn = self._backend.get_conn()
        try:
            now = datetime.now(UTC).isoformat()
            conn.execute(
                self._sql(
                    "UPDATE supervisor_runs SET state = 'completed', "
                    "completed_at = ?, turn_count = ? WHERE run_id = ?"
                ),
                (now, turn_count, run_id),
            )
            conn.commit()
        finally:
            self._backend.put_conn(conn)

    def _fail_run_sync(self, run_id: str, error: str) -> None:
        conn = self._backend.get_conn()
        try:
            now = datetime.now(UTC).isoformat()
            conn.execute(
                self._sql(
                    "UPDATE supervisor_runs SET state = 'failed', "
                    "completed_at = ?, error = ? WHERE run_id = ?"
                ),
                (now, error, run_id),
            )
            conn.commit()
        finally:
            self._backend.put_conn(conn)

    def _get_run_sync(self, run_id: str) -> dict[str, Any] | None:
        conn = self._backend.get_conn()
        try:
            cur = conn.execute(
                self._sql("SELECT * FROM supervisor_runs WHERE run_id = ?"),
                (run_id,),
            )
            row = cur.fetchone()
        finally:
            self._backend.put_conn(conn)
        if row is None:
            return None
        return dict(row)

    def _get_events_sync(self, run_id: str) -> list[dict[str, Any]]:
        conn = self._backend.get_conn()
        try:
            cur = conn.execute(
                self._sql(
                    "SELECT * FROM supervisor_events WHERE run_id = ? ORDER BY seq"
                ),
                (run_id,),
            )
            rows = cur.fetchall()
        finally:
            self._backend.put_conn(conn)
        return [dict(r) for r in rows]

    def _persist_events_sync(
        self,
        run_id: str,
        events: list[SupervisorEvent],
    ) -> None:
        conn = self._backend.get_conn()
        try:
            for seq, event in enumerate(events, start=1):
                conn.execute(
                    self._sql(
                        "INSERT OR IGNORE INTO supervisor_events "
                        "(run_id, seq, kind, payload, timestamp) "
                        "VALUES (?, ?, ?, ?, ?)"
                    ),
                    (
                        run_id,
                        seq,
                        event.kind.value,
                        json.dumps(event.payload, default=str),
                        event.timestamp.isoformat(),
                    ),
                )
            conn.commit()
        finally:
            self._backend.put_conn(conn)

    def _store_prompt_snapshot_sync(
        self,
        run_id: str,
        prompt_snapshot: Any,
    ) -> None:
        conn = self._backend.get_conn()
        try:
            snapshot_id = str(uuid.uuid4())
            sections_json = json.dumps(
                [
                    {"name": s.name, "content": s.content, "tokens": s.token_estimate}
                    for s in prompt_snapshot.sections
                ],
                default=str,
            )
            conn.execute(
                self._sql(
                    "INSERT OR REPLACE INTO prompt_snapshots "
                    "(snapshot_id, run_id, prompt_hash, sections_json, token_count, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)"
                ),
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
        finally:
            self._backend.put_conn(conn)

    def _recover_orphaned_runs_sync(self) -> int:
        conn = self._backend.get_conn()
        try:
            now = datetime.now(UTC).isoformat()
            cursor = conn.execute(
                self._sql(
                    "UPDATE supervisor_runs SET state = 'failed', "
                    "completed_at = ?, error = 'crash_recovery' "
                    "WHERE state NOT IN ('completed', 'failed', 'idle') "
                    "AND completed_at IS NULL"
                ),
                (now,),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            self._backend.put_conn(conn)

    def close(self) -> None:
        """Close all connections."""
        self._lock.close()
        self._tool_store.close()
        self._backend.close()
