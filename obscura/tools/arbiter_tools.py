"""obscura.tools.arbiter_tools — Agent-facing tools for the Arbiter judge.

Provides tools for agents to:
  - Query Arbiter status and recent verdicts
  - Appeal a DENY verdict with reasoning
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

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


def get_arbiter_tool_specs() -> list[ToolSpec]:
    """Return Arbiter tool specs for registration."""
    return [
        cast("ToolSpec", arbiter_status.spec),
        cast("ToolSpec", arbiter_appeal.spec),
    ]
