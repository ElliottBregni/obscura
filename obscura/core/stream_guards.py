"""Per-task tool-call guards shared across all backends.

The model can — and observably does — call the same tool with identical
arguments repeatedly within a single user→agent task, fabricating a
different summary each time. Two backends drive their own internal tool
loops (Copilot, Claude via the agent SDK; Codex via the threads API);
others drive the loop via :class:`obscura.core.agent_loop_v2.AgentLoopV2`.
The defenses in this module apply uniformly:

* **Dedup guard** — refuse a 2nd identical ``(tool_name, args)`` call within
  the active task. The model gets a structured failure telling it the prior
  result is already in context.
* **Budget guard** — cap total tool calls per task. Refuse further calls
  once the cap is hit.

Both guards are driven by a :class:`contextvars.ContextVar` that the entry
point (each backend's stream method or the agent loop's ``run``) binds via
:func:`bind_stream_log`. ContextVars are propagated to child asyncio tasks,
so SDK-spawned tool tasks see the same log dict the entry point bound.

When no log is bound (e.g. tools invoked outside any stream lifecycle, or
in tests), the guards fail open — :func:`check_stream_guards` returns
``None`` and the call proceeds.

Each backend converts the refusal payload into its own provider-shaped
result envelope (``CopilotToolResult`` for Copilot, MCP-shaped dict for
Claude, etc.) — see :func:`refusal_text` for a JSON serialization that
all backends can wrap.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

# Per-task log: maps (tool_name, args_hash) → invocation count.
STREAM_TOOL_LOG: ContextVar[dict[tuple[str, str], int] | None] = ContextVar(
    "obscura_stream_tool_log",
    default=None,
)

# 1st call OK; 2nd identical call refused.
MAX_DUPLICATE_CALLS = 1

# Hard cap on total tool calls per user→agent task.
MAX_TOTAL_CALLS = 50


def hash_args(args: dict[str, Any]) -> str:
    """Stable hash of tool arguments for dedup."""
    try:
        return json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        logger.debug("hash_args: json.dumps failed, using repr fallback", exc_info=True)
        return repr(sorted(args.items()))


def check_stream_guards(
    name: str,
    args: dict[str, Any],
) -> dict[str, Any] | None:
    """Apply dedup and budget guards.

    Returns a refusal payload (dict suitable for ``json.dumps``) if the call
    should be refused; returns ``None`` if the call may proceed. When the
    call may proceed, the active log is mutated to record this invocation.

    Fails open when no log is bound.
    """
    log = STREAM_TOOL_LOG.get()
    if log is None:
        return None
    total_calls = sum(log.values())
    if total_calls >= MAX_TOTAL_CALLS:
        logger.warning(
            "Tool budget exhausted for %s after %d calls", name, total_calls,
        )
        return {
            "ok": False,
            "error": "tool_budget_exhausted",
            "tool": name,
            "total_calls": total_calls,
            "limit": MAX_TOTAL_CALLS,
            "message": (
                f"Tool-call budget exhausted ({total_calls} calls in this task, "
                f"limit {MAX_TOTAL_CALLS}). Stop calling tools and respond with "
                "what is already in your context."
            ),
        }
    cache_key = (name, hash_args(args))
    seen = log.get(cache_key, 0)
    if seen >= MAX_DUPLICATE_CALLS:
        logger.warning(
            "Duplicate tool call refused: %s (seen %d times)", name, seen,
        )
        return {
            "ok": False,
            "error": "duplicate_call_in_same_turn",
            "tool": name,
            "args": args,
            "prior_call_count": seen,
            "message": (
                f"You already called {name} with these exact arguments in this "
                "task. The prior result is in your context above. Use that "
                "result — do not re-call the tool. If the prior result was "
                "insufficient, call a different tool or change the arguments."
            ),
        }
    log[cache_key] = seen + 1
    return None


def refusal_text(payload: dict[str, Any]) -> str:
    """Serialize a refusal payload to JSON text for wrapping into a provider result."""
    return json.dumps(payload)


@contextmanager
def bind_stream_log() -> Iterator[dict[tuple[str, str], int]]:
    """Bind a fresh per-task log for the duration of the with-block.

    Usage::

        with bind_stream_log():
            # SDK / agent loop runs here; tool wrappers inside see the log.
            ...

    Idempotent — nested binds use the innermost log. (We let the inner bind
    win so a sub-task can opt into its own counters; if you'd prefer the
    outer log to span sub-tasks, just don't bind in the inner scope.)
    """
    log: dict[tuple[str, str], int] = {}
    token = STREAM_TOOL_LOG.set(log)
    try:
        yield log
    finally:
        STREAM_TOOL_LOG.reset(token)
