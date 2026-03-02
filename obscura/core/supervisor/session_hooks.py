"""
obscura.core.supervisor.session_hooks — Session-scoped hooks (first-class).

Hooks are persisted per session, replayed on resume, and recorded as
events in the supervisor log. This makes hooks observable, debuggable,
and replayable.

Key differences from HookRegistry:
- Hooks are persisted to SQLite (survive restarts)
- Hook invocations are logged as supervisor events
- Hooks are scoped to sessions (not global)
- Hooks have priority ordering
- Hook registrations are themselves events
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from obscura.core.supervisor.schema import init_supervisor_schema
from obscura.core.supervisor.types import (
    SupervisorEvent,
    SupervisorEventKind,
    SupervisorHookPoint,
)

logger = logging.getLogger(__name__)

# Hook callback types
BeforeHookFn = Callable[..., Awaitable[Any] | Any]
AfterHookFn = Callable[..., Awaitable[None] | None]


class SessionHookEntry:
    """A registered hook for a session."""

    __slots__ = (
        "hook_point",
        "hook_type",
        "handler_ref",
        "handler",
        "priority",
        "active",
    )

    def __init__(
        self,
        hook_point: SupervisorHookPoint,
        hook_type: str,  # "before" or "after"
        handler_ref: str,
        handler: BeforeHookFn | AfterHookFn | None = None,
        priority: int = 0,
        active: bool = True,
    ) -> None:
        self.hook_point = hook_point
        self.hook_type = hook_type
        self.handler_ref = handler_ref
        self.handler = handler
        self.priority = priority
        self.active = active


class SessionHookManager:
    """Manages session-scoped hooks with persistence and event logging.

    Usage::

        hooks = SessionHookManager(
            db_path="/tmp/supervisor.db",
            session_id="sess-1",
        )

        # Register a hook (persisted)
        hooks.register(
            hook_point=SupervisorHookPoint.PRE_TOOL_EXECUTION,
            hook_type="before",
            handler_ref="audit_tool_call",
            handler=my_handler,
            priority=10,
        )

        # Fire hooks
        result = await hooks.fire_before(
            SupervisorHookPoint.PRE_TOOL_EXECUTION,
            context={"tool_name": "bash", "args": {...}},
        )

        # Load persisted hooks on session resume
        hooks.load_from_db()
    """

    def __init__(
        self,
        db_path: str | Path,
        session_id: str,
        *,
        run_id: str = "",
    ) -> None:
        self._db_path = Path(db_path)
        self._session_id = session_id
        self._run_id = run_id
        self._local = threading.local()
        self._hooks: list[SessionHookEntry] = []
        self._events: list[SupervisorEvent] = []
        self._handler_map: dict[str, BeforeHookFn | AfterHookFn] = {}
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

    # -- registration --------------------------------------------------------

    def register(
        self,
        hook_point: SupervisorHookPoint,
        hook_type: str,
        handler_ref: str,
        handler: BeforeHookFn | AfterHookFn | None = None,
        *,
        priority: int = 0,
        persist: bool = True,
    ) -> SessionHookEntry:
        """Register a hook for this session.

        Args:
            hook_point: When the hook fires
            hook_type: "before" (can modify/suppress) or "after" (side-effects)
            handler_ref: Serializable reference (for persistence/replay)
            handler: Actual callable (not persisted — must be re-bound on resume)
            priority: Lower = fires first
            persist: Whether to persist to DB

        Returns:
            The created hook entry.
        """
        entry = SessionHookEntry(
            hook_point=hook_point,
            hook_type=hook_type,
            handler_ref=handler_ref,
            handler=handler,
            priority=priority,
        )
        self._hooks.append(entry)

        if handler:
            self._handler_map[handler_ref] = handler

        if persist:
            self._persist_hook(entry)

        self._emit_event(
            SupervisorEventKind.HOOK_REGISTERED,
            {
                "hook_point": hook_point.value,
                "hook_type": hook_type,
                "handler_ref": handler_ref,
                "priority": priority,
            },
        )

        logger.debug(
            "Registered hook: %s/%s handler=%s priority=%d",
            hook_point.value,
            hook_type,
            handler_ref,
            priority,
        )
        return entry

    def bind_handler(
        self,
        handler_ref: str,
        handler: BeforeHookFn | AfterHookFn,
    ) -> None:
        """Bind a callable to a handler_ref (for session resume)."""
        self._handler_map[handler_ref] = handler
        for entry in self._hooks:
            if entry.handler_ref == handler_ref:
                entry.handler = handler

    def unregister(self, handler_ref: str) -> bool:
        """Unregister a hook by handler_ref."""
        original_count = len(self._hooks)
        self._hooks = [h for h in self._hooks if h.handler_ref != handler_ref]
        removed = len(self._hooks) < original_count

        if removed:
            self._handler_map.pop(handler_ref, None)
            self._unpersist_hook(handler_ref)
            self._emit_event(
                SupervisorEventKind.HOOK_REMOVED,
                {"handler_ref": handler_ref},
            )

        return removed

    # -- firing --------------------------------------------------------------

    async def fire_before(
        self,
        hook_point: SupervisorHookPoint,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Fire all 'before' hooks for a hook point.

        Before hooks can modify the context or return None to suppress.

        Returns:
            Modified context, or None if suppressed.
        """
        ctx = dict(context or {})
        entries = self._get_hooks(hook_point, "before")

        for entry in entries:
            if not entry.handler:
                continue

            try:
                result = entry.handler(ctx)
                if inspect.isawaitable(result):
                    result = await result

                self._emit_event(
                    SupervisorEventKind.HOOK_FIRED,
                    {
                        "hook_point": hook_point.value,
                        "hook_type": "before",
                        "handler_ref": entry.handler_ref,
                        "result": "suppress" if result is None else "allow",
                    },
                )

                if result is None:
                    return None
                if isinstance(result, dict):
                    ctx = result

            except Exception:
                logger.exception(
                    "Before hook %s failed", entry.handler_ref
                )
                self._emit_event(
                    SupervisorEventKind.HOOK_FIRED,
                    {
                        "hook_point": hook_point.value,
                        "handler_ref": entry.handler_ref,
                        "result": "error",
                    },
                )

        return ctx

    async def fire_after(
        self,
        hook_point: SupervisorHookPoint,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Fire all 'after' hooks for a hook point.

        After hooks observe only — return values are ignored.
        """
        ctx = dict(context or {})
        entries = self._get_hooks(hook_point, "after")

        for entry in entries:
            if not entry.handler:
                continue

            try:
                result = entry.handler(ctx)
                if inspect.isawaitable(result):
                    await result

                self._emit_event(
                    SupervisorEventKind.HOOK_FIRED,
                    {
                        "hook_point": hook_point.value,
                        "hook_type": "after",
                        "handler_ref": entry.handler_ref,
                        "result": "ok",
                    },
                )

            except Exception:
                logger.exception(
                    "After hook %s failed", entry.handler_ref
                )

    # -- persistence ---------------------------------------------------------

    def load_from_db(self) -> int:
        """Load persisted hooks for this session. Returns count loaded."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT hook_point, hook_type, handler_ref, priority, active "
            "FROM session_hooks WHERE session_id = ? AND active = 1 "
            "ORDER BY priority",
            (self._session_id,),
        ).fetchall()

        count = 0
        for row in rows:
            try:
                hook_point = SupervisorHookPoint(row["hook_point"])
            except ValueError:
                logger.warning("Unknown hook point: %s", row["hook_point"])
                continue

            handler = self._handler_map.get(row["handler_ref"])
            entry = SessionHookEntry(
                hook_point=hook_point,
                hook_type=row["hook_type"],
                handler_ref=row["handler_ref"],
                handler=handler,
                priority=row["priority"],
                active=bool(row["active"]),
            )
            self._hooks.append(entry)
            count += 1

        logger.debug("Loaded %d hooks from DB for session %s", count, self._session_id)
        return count

    def _persist_hook(self, entry: SessionHookEntry) -> None:
        """Persist a hook to DB (sync)."""
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO session_hooks "
            "(session_id, hook_point, hook_type, handler_ref, priority, "
            " active, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (
                self._session_id,
                entry.hook_point.value,
                entry.hook_type,
                entry.handler_ref,
                entry.priority,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()

    def _unpersist_hook(self, handler_ref: str) -> None:
        """Mark a hook as inactive in DB."""
        conn = self._conn()
        conn.execute(
            "UPDATE session_hooks SET active = 0 "
            "WHERE session_id = ? AND handler_ref = ?",
            (self._session_id, handler_ref),
        )
        conn.commit()

    # -- internal ------------------------------------------------------------

    def _get_hooks(
        self,
        hook_point: SupervisorHookPoint,
        hook_type: str,
    ) -> list[SessionHookEntry]:
        """Get matching hooks, sorted by priority (ascending)."""
        matching = [
            h
            for h in self._hooks
            if h.hook_point == hook_point
            and h.hook_type == hook_type
            and h.active
        ]
        matching.sort(key=lambda h: h.priority)
        return matching

    @property
    def hook_count(self) -> int:
        return len([h for h in self._hooks if h.active])

    @property
    def events(self) -> list[SupervisorEvent]:
        return list(self._events)

    def _emit_event(
        self,
        kind: SupervisorEventKind,
        payload: dict[str, Any],
    ) -> None:
        self._events.append(
            SupervisorEvent(
                kind=kind,
                run_id=self._run_id,
                session_id=self._session_id,
                payload=payload,
            )
        )

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None
