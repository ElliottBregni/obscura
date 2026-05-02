"""obscura.tools.arbiter_tools — Agent-facing tools for the Arbiter judge.

Provides tools for agents to:
  - Query Arbiter status and recent verdicts
  - Query the live arbiter.db store with filtering
  - Appeal a DENY verdict with reasoning
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from obscura.core.paths import resolve_obscura_home
from obscura.core.tools import tool

if TYPE_CHECKING:
    from obscura.core.types import ToolSpec


@tool(
    "arbiter_status",
    "Show Arbiter judge status: recent verdicts, score distribution, active retries.",
    {
        "type": "object",
        "properties": {
            "last_n": {
                "type": "integer",
                "description": "Number of recent verdicts to show (default 10).",
            },
        },
    },
)
async def arbiter_status(last_n: int = 10) -> str:
    try:
        from obscura.arbiter.hooks import get_engine

        engine = get_engine()
        if engine is None:
            return json.dumps(
                {"ok": True, "running": False, "message": "Arbiter not active."}
            )

        status = engine.status()
        # Include recent events.
        recent = engine.events[-last_n:]
        status["recent_verdicts"] = [
            {
                "kind": e.kind.value,
                "verdict": e.verdict.value,
                "target_id": e.target_id,
                "score": e.score.composite,
                "feedback": e.score.feedback[:200] if e.score.feedback else "",
                "timestamp": e.timestamp.isoformat(),
            }
            for e in reversed(recent)
        ]
        return json.dumps({"ok": True, **status})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


@tool(
    "arbiter_appeal",
    "Appeal a DENY verdict. Provide task_id/goal_id and reasoning for re-evaluation.",
    {
        "type": "object",
        "properties": {
            "target_id": {
                "type": "string",
                "description": "The target that was denied (e.g. 'task:abc123').",
            },
            "reasoning": {
                "type": "string",
                "description": "Why the verdict should be reconsidered.",
            },
        },
        "required": ["target_id", "reasoning"],
    },
)
async def arbiter_appeal(target_id: str, reasoning: str) -> str:
    try:
        from obscura.arbiter.hooks import get_engine

        engine = get_engine()
        if engine is None:
            return json.dumps({"ok": False, "error": "Arbiter not active."})

        # Find the most recent DENY event for this target.
        denied_event = None
        for event in reversed(engine.events):
            if event.target_id == target_id and event.verdict.value == "deny":
                denied_event = event
                break

        if denied_event is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"No recent DENY verdict found for {target_id}.",
                }
            )

        # Re-evaluate with the appeal context injected.
        # Force LLM judge for appeals.
        from dataclasses import replace as dc_replace

        appeal_config = dc_replace(engine.config, judge_mode="always")
        original_config = engine._config
        engine._config = appeal_config

        try:
            score = await engine.evaluate(
                denied_event.kind,
                {
                    "output_text": reasoning,
                    "summary": f"Appeal for {target_id}: {reasoning}",
                    "appeal": True,
                },
            )
        finally:
            engine._config = original_config

        return json.dumps(
            {
                "ok": True,
                "target_id": target_id,
                "new_verdict": score.verdict.value,
                "new_score": score.composite,
                "feedback": score.feedback,
            }
        )
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


@tool(
    "query_arbiter_verdicts",
    (
        "Query recent arbiter verdicts from the live store. "
        "Returns verdicts filtered by session, kind, or verdict type. "
        "Read-only — never writes to the store."
    ),
    {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of verdicts to return (default 20, max 100).",
            },
            "session_id": {
                "type": "string",
                "description": "Filter to a specific session ID.",
            },
            "verdict": {
                "type": "string",
                "description": "Filter by verdict type: 'accept', 'revise', 'deny', or 'kill'.",
                "enum": ["accept", "revise", "deny", "kill"],
            },
            "kind": {
                "type": "string",
                "description": (
                    "Filter by event kind: 'tool_call', 'model_turn', "
                    "'task_complete', or 'goal_transition'."
                ),
                "enum": ["tool_call", "model_turn", "task_complete", "goal_transition"],
            },
            "min_score": {
                "type": "number",
                "description": "Only return verdicts with composite score >= this value.",
            },
        },
    },
)
async def query_arbiter_verdicts(
    limit: int = 20,
    session_id: str = "",
    verdict: str = "",
    kind: str = "",
    min_score: float | None = None,
) -> str:
    """Query arbiter verdicts from the live SQLite store with optional filters."""
    try:
        db_path: Path = resolve_obscura_home() / "arbiter.db"
        if not db_path.exists():
            return json.dumps({"ok": True, "verdicts": [], "message": "No arbiter.db found."})

        limit = max(1, min(limit, 100))

        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            clauses: list[str] = []
            params: list[Any] = []

            if session_id:
                clauses.append("session_id = ?")
                params.append(session_id)
            if verdict:
                clauses.append("verdict = ?")
                params.append(verdict)
            if kind:
                clauses.append("kind = ?")
                params.append(kind)
            if min_score is not None:
                clauses.append("composite >= ?")
                params.append(min_score)

            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(limit)

            rows = conn.execute(
                f"SELECT * FROM verdicts {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()

            verdicts_out = []
            for row in rows:
                r = dict(row)
                # Decode JSON fields for readability
                for field_name in ("details", "metadata"):
                    if isinstance(r.get(field_name), str):
                        try:
                            r[field_name] = json.loads(r[field_name])
                        except (json.JSONDecodeError, TypeError):
                            pass
                verdicts_out.append(r)

            return json.dumps({"ok": True, "count": len(verdicts_out), "verdicts": verdicts_out})
        finally:
            conn.close()
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def get_arbiter_tool_specs() -> list[ToolSpec]:
    """Return Arbiter tool specs for registration."""
    return [
        cast("ToolSpec", arbiter_status.spec),
        cast("ToolSpec", arbiter_appeal.spec),
        cast("ToolSpec", query_arbiter_verdicts.spec),
    ]
