"""Session and agent-state tools (todos, history, plan mode, sleep)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar, cast

from obscura.core.tool_context import current_tool_context
from obscura.core.tools import tool
from obscura.tools.system._policy import Policy


class Session:
    """Session-state tool namespace (token usage, todos, plan-mode, history)."""

    # ------------------------------------------------------------------
    # Class-level state (preserved across calls; mutated by setters/tools)
    # ------------------------------------------------------------------

    # Session-level token usage tracker. Updated by the agent loop after each
    # turn (via ``Session.update_token_usage``) so the LLM can introspect.
    token_usage: ClassVar[dict[str, int]] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "context_window": 0,
        "compact_threshold": 0,
    }

    # Plan-mode toggle — callbacks set by the CLI layer via the setter
    # classmethods. The ``enter_plan_mode``/``exit_plan_mode`` tools read them
    # at call time.
    permission_mode_callback: ClassVar[Any] = None
    plan_approval_callback: ClassVar[Any] = None

    # Module-level message history reference (set by REPL).
    snip_message_history: ClassVar[list[Any] | None] = None

    # Current todo list (mutated by ``todo_write``).
    todo_items: ClassVar[list[Any]] = []

    # ------------------------------------------------------------------
    # Setters / state mutators
    # ------------------------------------------------------------------

    @classmethod
    def update_token_usage(
        cls,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        context_window: int = 0,
        compact_threshold: int = 0,
    ) -> None:
        """Called by the agent loop / CLI to keep the token tracker current."""
        if input_tokens:
            cls.token_usage["input_tokens"] = input_tokens
        if output_tokens:
            cls.token_usage["output_tokens"] = output_tokens
        cls.token_usage["total_tokens"] = (
            cls.token_usage["input_tokens"] + cls.token_usage["output_tokens"]
        )
        if context_window:
            cls.token_usage["context_window"] = context_window
        if compact_threshold:
            cls.token_usage["compact_threshold"] = compact_threshold

    @classmethod
    def set_permission_mode_callback(cls, cb: Any) -> None:
        """Register a callable that switches the agent's permission mode."""
        cls.permission_mode_callback = cb

    @classmethod
    def set_plan_approval_callback(cls, cb: Any) -> None:
        """Register an async callable that gates exit_plan_mode on user approval."""
        cls.plan_approval_callback = cb

    @classmethod
    def set_snip_message_history(cls, history: list[Any]) -> None:
        """Set the message history reference for the snip tool."""
        cls.snip_message_history = history

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @staticmethod
    @tool(
        "context_window_status",
        (
            "Check the current context window usage. Returns token counts, "
            "percentage used, and whether compaction is recommended. "
            "Call this to monitor context usage during long conversations."
        ),
        {
            "type": "object",
            "properties": {},
        },
    )
    async def context_window_status() -> str:
        window = Session.token_usage.get("context_window", 0) or 200_000
        total = Session.token_usage.get("total_tokens", 0)
        input_t = Session.token_usage.get("input_tokens", 0)
        output_t = Session.token_usage.get("output_tokens", 0)
        compact_at = Session.token_usage.get("compact_threshold", 0) or int(
            window * 0.60
        )
        pct = round(total / window * 100, 1) if window else 0.0

        return json.dumps(
            {
                "ok": True,
                "input_tokens": input_t,
                "output_tokens": output_t,
                "total_tokens": total,
                "context_window": window,
                "compact_threshold": compact_at,
                "percent_used": pct,
                "should_compact": total > compact_at,
                "status": (
                    "critical" if pct > 80 else "warning" if pct > 60 else "healthy"
                ),
            },
        )

    @staticmethod
    @tool(
        "todo_write",
        (
            "Create or update a task list to track progress. "
            "Accepts a JSON array of todo objects, each with 'content' (str), "
            "'status' ('pending'|'in_progress'|'completed'), and 'activeForm' (str, "
            "present-tense description shown while task runs). "
            "Replaces the full list each call. Returns the updated list."
        ),
        {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                            "activeForm": {"type": "string"},
                        },
                        "required": ["content", "status"],
                    },
                    "description": "The full todo list (replaces previous).",
                },
            },
            "required": ["todos"],
        },
    )
    async def todo_write(todos: Any = None) -> str:
        if todos is None:
            todos = []
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except (json.JSONDecodeError, ValueError):
                return json.dumps({"ok": False, "error": "todos must be a JSON array"})
        if not isinstance(todos, list):
            return json.dumps({"ok": False, "error": "todos must be a JSON array"})
        items: list[Any] = []
        for raw in cast(list[object], todos):
            if not isinstance(raw, dict):
                continue
            t = cast(dict[str, object], raw)
            items.append(
                {
                    "content": str(t.get("content", "")),
                    "status": str(t.get("status", "pending")),
                    "activeForm": str(t.get("activeForm", "")),
                }
            )
        Session.todo_items = items
        return json.dumps(
            {"ok": True, "count": len(Session.todo_items), "todos": Session.todo_items}
        )

    @staticmethod
    @tool(
        "report_intent",
        "Report the agent's current intent or plan before acting. Call this before starting any significant task to surface what you are about to do.",
        {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": "The agent's current intent or high-level plan",
                },
            },
            "required": ["intent"],
        },
    )
    async def report_intent(intent: str) -> str:
        return json.dumps({"ok": True, "intent": intent})

    @staticmethod
    @tool(
        "enter_plan_mode",
        "Switch to plan mode. In plan mode only read-only tools are allowed. "
        "Use this when you need to explore the codebase and design an "
        "implementation plan before making changes.",
        {
            "type": "object",
            "properties": {},
        },
    )
    async def enter_plan_mode() -> str:
        ctx = current_tool_context()
        cb = ctx.permission_mode_callback if ctx is not None else None
        if cb is None:
            cb = Session.permission_mode_callback
        if cb is not None:
            try:
                cb("plan")
            except Exception as exc:
                return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps({"ok": True, "mode": "plan"})

    @staticmethod
    @tool(
        "exit_plan_mode",
        "Exit plan mode and return to default permissions so that write and "
        "execute tools become available again.  Requires user approval via the "
        "renderer before the mode switch takes effect.",
        {
            "type": "object",
            "properties": {
                "plan_summary": {
                    "type": "string",
                    "description": "Short summary of the plan being approved.",
                },
            },
        },
    )
    async def exit_plan_mode(plan_summary: str = "") -> str:
        ctx = current_tool_context()
        approval_cb = ctx.plan_approval_callback if ctx is not None else None
        if approval_cb is None:
            approval_cb = Session.plan_approval_callback
        mode_cb = ctx.permission_mode_callback if ctx is not None else None
        if mode_cb is None:
            mode_cb = Session.permission_mode_callback

        # If a renderer approval callback is registered, gate on it.
        if approval_cb is not None:
            try:
                approved = approval_cb(plan_summary)
                if asyncio.iscoroutine(approved) or asyncio.isfuture(approved):
                    approved = await approved
                if not approved:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "Plan not approved by user. Staying in plan mode.",
                            "mode": "plan",
                        }
                    )
            except Exception as exc:
                return json.dumps({"ok": False, "error": str(exc)})

        if mode_cb is not None:
            try:
                mode_cb("default")
            except Exception as exc:
                return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps({"ok": True, "mode": "default"})

    @staticmethod
    @tool(
        "history_snip",
        (
            "Remove specific message segments from the conversation history "
            "to free context window space. Specify a range of turn indices to remove."
        ),
        {
            "type": "object",
            "properties": {
                "start_turn": {
                    "type": "integer",
                    "description": "First turn index to remove (0-based).",
                },
                "end_turn": {
                    "type": "integer",
                    "description": "Last turn index to remove (inclusive).",
                },
                "reason": {
                    "type": "string",
                    "description": "Why these turns are being removed.",
                },
            },
            "required": ["start_turn", "end_turn"],
        },
    )
    async def history_snip(
        start_turn: int,
        end_turn: int,
        reason: str = "",
    ) -> str:
        ctx = current_tool_context()
        history = ctx.history if ctx is not None else None
        if history is None:
            history = Session.snip_message_history
        if history is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": "no_history",
                    "detail": "Message history not available",
                },
            )

        try:
            start_turn = int(start_turn)
        except (TypeError, ValueError):
            start_turn = 0
        try:
            end_turn = int(end_turn)
        except (TypeError, ValueError):
            end_turn = 0

        total = len(history)
        if start_turn < 0 or end_turn >= total or start_turn > end_turn:
            return json.dumps(
                {
                    "ok": False,
                    "error": "invalid_range",
                    "detail": f"Range {start_turn}-{end_turn} invalid (history has {total} entries)",
                },
            )

        # Remove the specified range.
        removed_count = end_turn - start_turn + 1
        del history[start_turn : end_turn + 1]

        return json.dumps(
            {
                "ok": True,
                "removed_turns": removed_count,
                "remaining_turns": len(history),
                "reason": reason,
            },
        )

    @staticmethod
    @tool(
        "sleep",
        (
            "Pause execution for the given number of seconds. Useful when you need "
            "to wait for an external process to settle before continuing."
        ),
        {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "How long to sleep, in seconds (max 60).",
                },
            },
            "required": ["seconds"],
        },
    )
    async def sleep(seconds: float) -> str:
        try:
            s = float(seconds)
        except (TypeError, ValueError):
            return Policy.json_error("invalid_seconds", value=str(seconds))
        if s < 0:
            return Policy.json_error("invalid_seconds", detail="must be >= 0")
        s = min(s, 60.0)
        await asyncio.sleep(s)
        return json.dumps({"ok": True, "slept_seconds": s})
