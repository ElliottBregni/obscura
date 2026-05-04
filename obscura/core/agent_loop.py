# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportOptionalMemberAccess=false, reportImplicitOverride=false, reportPrivateUsage=false, reportUnusedFunction=false, reportMissingTypeArgument=false, reportConstantRedefinition=false, reportUnnecessaryTypeIgnoreComment=false
"""
obscura.agent_loop — Iterative agent loop with tool execution.

Drives the model in a loop: send prompt → stream response → detect tool
calls → execute tools → feed results back → repeat until the model
produces a final text response or *max_turns* is reached.

Works with all backends (Copilot, Claude, OpenAI, LocalLLM).

Usage::

    from obscura.core.agent_loop import AgentLoop
    from obscura.core.hooks import HookRegistry
    from obscura.core.compaction import compact_history
from obscura.core.event_store import SQLiteEventStore
    from obscura.core.types import AgentEventKind

    hooks = HookRegistry()
    store = SQLiteEventStore("/tmp/events.db")

    loop = AgentLoop(
        backend, tool_registry,
        hooks=hooks, event_store=store,
    )

    async for event in loop.run("Fix the auth bug", session_id="sess-1"):
        match event.kind:
            case AgentEventKind.TEXT_DELTA:
                print(event.text, end="")
            case AgentEventKind.TOOL_CALL:
                print(f"[tool] {event.tool_name}({event.tool_input})")
            case AgentEventKind.TOOL_RESULT:
                print(f"[result] {event.tool_result[:80]}")
            case AgentEventKind.AGENT_DONE:
                print("\\nDone!")
"""

from __future__ import annotations

import asyncio
import enum
import difflib
import inspect
import json
import logging
import os
import time
import uuid
from collections.abc import Coroutine
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from collections.abc import AsyncIterator, Awaitable, Callable

from obscura.core.models.content import (
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    AgentEvent,
    AgentEventKind,
    BackendProtocol,
    ChunkKind,
    Message,
    Role,
    StreamChunk,
    ToolCallContext,
    ToolCallEnvelope,
    ToolCallInfo,
    ToolErrorType,
    ToolExecutionError,
    ToolResultEnvelope,
    ToolRouterCapable,
    ToolSpec,
)

from obscura.core.event_store import EventRecord, EventStoreProtocol, SessionStatus
from obscura.core.hooks import HookRegistry
from obscura.core.predictive_tools import (
    PredictiveToolCache,
    ToolPredictor,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool concurrency
# ---------------------------------------------------------------------------

MAX_TOOL_CONCURRENCY: int = int(os.environ.get("OBSCURA_MAX_TOOL_CONCURRENCY", "10"))

# Maximum times stop hooks can suppress the stop and continue the loop.
# Prevents infinite loops from greedy hooks that always suppress the stop.
MAX_STOP_HOOK_CONTINUATIONS: int = 5

# ---------------------------------------------------------------------------
# Max output tokens recovery
# ---------------------------------------------------------------------------

MAX_OUTPUT_TOKENS_RETRIES: int = 3
DEFAULT_MAX_TOKENS: int = 16384  # 16k default
ESCALATED_MAX_TOKENS: int = 65536  # 64k max escalation


# ---------------------------------------------------------------------------
# Streaming tool executor
# ---------------------------------------------------------------------------


class StreamingToolExecutor:
    """Executes tools as they stream in from the model, not after the full response.

    .. warning::
       :attr:`seen_calls` is a cross-retry dedup map keyed by tool_use_id. It
       is **load-bearing for correctness**, not just performance: it prevents
       double-execution of side-effecting tools (e.g. ``git commit``) when an
       SDK stream is interrupted and the turn is retried. Any rewrite of this
       executor (Stage B DAG scheduler) MUST preserve the same semantic —
       check tool_use_id against a same-turn cache at dispatch time before
       invoking the handler.

    When a tool_use block finishes streaming, it is immediately handed to the
    executor via :meth:`add_tool`.  Concurrency-safe tools run in parallel;
    tools with side effects run alone.  Results are always returned in
    submission order via :meth:`wait_for_all`.
    """

    def __init__(
        self,
        tool_lookup: Callable[[str], ToolSpec | None],
        execute_tool: Callable[
            [ToolCallInfo, dict[str, ToolResultEnvelope]],
            Awaitable[ToolResultEnvelope],
        ],
        max_concurrency: int = MAX_TOOL_CONCURRENCY,
    ) -> None:
        self._tool_lookup = tool_lookup
        self._execute_tool = execute_tool
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._pending: list[ToolCallInfo] = []  # tools waiting to start
        self.in_flight: dict[str, asyncio.Task[None]] = {}  # tool_use_id -> task
        self._completed: dict[str, ToolResultEnvelope] = {}  # tool_use_id -> result
        self.order: list[str] = []  # tool_use_ids in submission order
        self._tool_call_map: dict[str, ToolCallInfo] = {}  # tool_use_id -> ToolCallInfo
        self._all_done = asyncio.Event()
        self._safe_in_flight_count: int = (
            0  # how many concurrency-safe tools are running
        )
        # Cross-retry dedup keyed by tool_use_id. LOAD-BEARING FOR CORRECTNESS:
        # if a stream is interrupted mid-turn and the turn retries, this
        # prevents executing side-effecting tools (git commit, write_file, …)
        # twice for the same tool_use_id. Any executor rewrite must preserve
        # this — check tool_use_id at dispatch time, return cached result on
        # hit. Cleared per turn (see _seen_calls_for_retry around line 1063).
        self.seen_calls: dict[str, ToolResultEnvelope] = {}
        self.abort_event = asyncio.Event()  # sibling abort signal
        self._closed = False  # set when stream errors; reject further adds

    def add_tool(self, tc: ToolCallInfo) -> None:
        """Called when a tool_use block finishes streaming.

        Starts execution immediately if concurrency allows, otherwise queues.
        """
        if self._closed:
            return
        self.order.append(tc.tool_use_id)
        self._tool_call_map[tc.tool_use_id] = tc

        spec = self._tool_lookup(tc.name)
        is_safe = spec is not None and spec.is_concurrency_safe()

        if is_safe and self._all_in_flight_are_safe():
            # Start immediately in parallel
            self._start_tool(tc)
        else:
            # Queue it -- will start when current batch completes
            self._pending.append(tc)
            self._process_queue()

    def close(self) -> None:
        """Mark executor as closed -- no more tools will be accepted."""
        self._closed = True

    def _start_tool(self, tc: ToolCallInfo) -> None:
        """Launch a tool execution task."""
        spec = self._tool_lookup(tc.name)
        is_safe = spec is not None and spec.is_concurrency_safe()
        if is_safe:
            self._safe_in_flight_count += 1
        task: asyncio.Task[None] = asyncio.create_task(self._run_tool(tc))
        self.in_flight[tc.tool_use_id] = task

    async def _run_tool(self, tc: ToolCallInfo) -> None:
        """Execute a single tool with semaphore limiting."""
        spec = self._tool_lookup(tc.name)
        is_safe = spec is not None and spec.is_concurrency_safe()
        result: ToolResultEnvelope = ToolResultEnvelope(
            call_id=tc.tool_use_id,
            tool=tc.name,
            status="error",
            error=ToolExecutionError(
                type=ToolErrorType.UNKNOWN,
                message="Tool execution did not produce a result.",
                safe_to_retry=False,
            ),
            tool_use_id=tc.tool_use_id,
            raw=tc.raw,
        )
        try:
            async with self._semaphore:
                if self.abort_event.is_set():
                    # Abort signalled -- return an error result
                    result = ToolResultEnvelope(
                        call_id=tc.tool_use_id,
                        tool=tc.name,
                        status="error",
                        error=ToolExecutionError(
                            type=ToolErrorType.UNKNOWN,
                            message="Aborted: sibling tool failed.",
                            safe_to_retry=False,
                        ),
                        tool_use_id=tc.tool_use_id,
                        raw=tc.raw,
                    )
                else:
                    result = await self._execute_tool(tc, self.seen_calls)
        except Exception as exc:
            logger.warning("StreamingToolExecutor: tool %s raised: %s", tc.name, exc)
            result = ToolResultEnvelope(
                call_id=tc.tool_use_id,
                tool=tc.name,
                status="error",
                error=ToolExecutionError(
                    type=ToolErrorType.UNKNOWN,
                    message=str(exc),
                    safe_to_retry=False,
                ),
                tool_use_id=tc.tool_use_id,
                raw=tc.raw,
            )
        finally:
            self._completed[tc.tool_use_id] = result
            del self.in_flight[tc.tool_use_id]
            if is_safe:
                self._safe_in_flight_count -= 1

            # Sibling abort: if a non-safe tool errors, signal others
            if not is_safe and result.status == "error":
                self.abort_event.set()

            self._process_queue()
            if not self.in_flight and not self._pending:
                self._all_done.set()

    def _process_queue(self) -> None:
        """Start pending tools if concurrency allows."""
        while self._pending:
            tc = self._pending[0]
            spec = self._tool_lookup(tc.name)
            is_safe = spec is not None and spec.is_concurrency_safe()

            if not self.in_flight:
                # Nothing in flight, start this tool
                self._pending.pop(0)
                self._start_tool(tc)
                if not is_safe:
                    break  # Non-concurrent tool runs alone
            elif is_safe and self._all_in_flight_are_safe():
                # All in-flight are safe, add another safe one
                self._pending.pop(0)
                self._start_tool(tc)
            else:
                break  # Must wait for current batch to finish

    def _all_in_flight_are_safe(self) -> bool:
        """Check if all currently in-flight tools are concurrency-safe."""
        if not self.in_flight:
            return True
        return self._safe_in_flight_count == len(self.in_flight)

    def get_completed_in_order(self) -> list[ToolResultEnvelope]:
        """Return completed results in submission order (non-blocking).

        Stops at the first tool_use_id that has not yet completed, so
        results are always contiguous from the start.
        """
        results: list[ToolResultEnvelope] = []
        for tid in self.order:
            if tid in self._completed:
                results.append(self._completed[tid])
            else:
                break
        return results

    async def wait_for_all(self) -> list[ToolResultEnvelope]:
        """Wait for all submitted tools to complete.

        Returns results in submission order.
        """
        if self.in_flight or self._pending:
            self._all_done.clear()
            await self._all_done.wait()
        return [self._completed[tid] for tid in self.order]

    def get_tool_calls_in_order(self) -> list[ToolCallInfo]:
        """Return all submitted ToolCallInfo objects in submission order."""
        return [self._tool_call_map[tid] for tid in self.order]

    @property
    def has_pending_or_in_flight(self) -> bool:
        """True if there are tools still pending or executing."""
        return bool(self._pending or self.in_flight)

    @property
    def has_tools(self) -> bool:
        """True if any tools were submitted."""
        return bool(self.order)


# ---------------------------------------------------------------------------
# Tool result truncation constants
# ---------------------------------------------------------------------------

MAX_TOOL_RESULT_SIZE = 200 * 1024  # 200 KB (measured in UTF-8 bytes)
TOOL_RESULT_CACHE_DIR = Path("~/.cache/obscura/tool-results").expanduser()


def _maybe_truncate_result(result: str, tool_name: str, tool_use_id: str) -> str:
    """If *result* exceeds MAX_TOOL_RESULT_SIZE bytes, write the full text to
    disk and return a truncated preview with a pointer to the cached file.

    The truncation cuts on the last newline boundary within the byte budget so
    we never split a multi-byte character.
    """
    encoded = result.encode("utf-8")
    if len(encoded) <= MAX_TOOL_RESULT_SIZE:
        return result

    # Ensure cache directory exists
    TOOL_RESULT_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Persist full result
    result_path = TOOL_RESULT_CACHE_DIR / f"{tool_use_id}.txt"
    result_path.write_text(result, encoding="utf-8")

    # Truncate to budget — decode back so we never land inside a multi-byte
    # sequence, then cut at the last newline for a clean break.
    truncated = encoded[:MAX_TOOL_RESULT_SIZE].decode("utf-8", errors="ignore")
    last_nl = truncated.rfind("\n")
    if last_nl > 0:
        truncated = truncated[:last_nl]

    return (
        f"{truncated}\n\n"
        f"[Result truncated — {len(result):,} chars total. "
        f"Full result saved to: {result_path}]"
    )


# Parameter name aliases for cross-provider compatibility.
# Maps provider-specific parameter names to canonical tool parameter names.
PARAMETER_ALIASES: dict[str, dict[str, str]] = {
    "write_text_file": {
        "content": "text",  # Copilot/OpenAI uses 'content', we use 'text'
        "file_path": "path",
        "filepath": "path",
    },
    "read_text_file": {
        "file_path": "path",
        "filepath": "path",
    },
    "append_text_file": {
        "content": "text",
        "file_path": "path",
        "filepath": "path",
    },
    "edit_text_file": {
        "file_path": "path",
        "filepath": "path",
        "old_string": "old_text",
        "new_string": "new_text",
        "oldText": "old_text",
        "newText": "new_text",
    },
    "run_shell": {
        "cmd": "script",
        "workdir": "cwd",
        "timeout": "timeout_seconds",
    },
    "todo_write": {
        "plan": "todos",
    },
    "find_files": {
        "head_limit": "max_results",
    },
    "task": {
        "subagent_type": "target",
    },
}


# ---------------------------------------------------------------------------
# Tool bridge — structural transforms for cross-backend compatibility
# ---------------------------------------------------------------------------
# These handle cases where simple parameter renames aren't enough — the
# input or output shape is fundamentally different between backends.
# Each entry is a tuple of (input_transform, output_transform).
# Either can be None to skip that direction.


def _bridge_grep_input(inputs: dict[str, Any]) -> dict[str, Any]:
    """Map Claude Code Grep flags to grep_files canonical params."""
    # -i (case insensitive) → case_sensitive=False
    if "-i" in inputs:
        val = inputs.pop("-i")
        if val and "case_sensitive" not in inputs:
            inputs["case_sensitive"] = False
    # -A, -B, -C context flags
    if "-A" in inputs:
        inputs.setdefault("after_context", inputs.pop("-A"))
    if "-B" in inputs:
        inputs.setdefault("before_context", inputs.pop("-B"))
    if "-C" in inputs:
        inputs.setdefault("context", inputs.pop("-C"))
    # -n (line numbers) — always on in Obscura, just drop it
    inputs.pop("-n", None)
    return inputs


def _bridge_task_input(inputs: dict[str, Any]) -> dict[str, Any]:
    """Map Claude Code Agent params to task tool."""
    # Drop unsupported params that would cause TypeError
    inputs.pop("description", None)
    inputs.pop("isolation", None)
    inputs.pop("run_in_background", None)
    inputs.pop("model", None)
    return inputs


def _bridge_run_shell_input(inputs: dict[str, Any]) -> dict[str, Any]:
    """Map Claude Code Bash params to run_shell."""
    inputs.pop("dangerouslyDisableSandbox", None)
    return inputs


def _bridge_todo_write_input(inputs: dict[str, Any]) -> dict[str, Any]:
    """Map Codex update_plan payloads to todo_write items."""
    todos_raw = inputs.get("todos")
    if not isinstance(todos_raw, list):
        return inputs
    todos = cast(list[Any], todos_raw)

    normalized: list[dict[str, str]] = []
    for raw_item in todos:
        if not isinstance(raw_item, dict):
            continue
        raw = cast(dict[str, Any], raw_item)
        content = raw.get("content") or raw.get("step") or raw.get("task") or ""
        status = raw.get("status") or "pending"
        active = raw.get("activeForm") or raw.get("active_form") or content
        normalized.append(
            {
                "content": str(content),
                "status": str(status),
                "activeForm": str(active),
            },
        )
    inputs["todos"] = normalized
    inputs.pop("explanation", None)
    return inputs


# Registry: canonical_tool_name → (input_transform, output_transform)
# output_transform receives the raw result string and returns a new string.
TOOL_BRIDGES: dict[
    str,
    tuple[
        Callable[[dict[str, Any]], dict[str, Any]] | None,
        Callable[[str], str] | None,
    ],
] = {
    "grep_files": (_bridge_grep_input, None),
    "task": (_bridge_task_input, None),
    "run_shell": (_bridge_run_shell_input, None),
    "todo_write": (_bridge_todo_write_input, None),
}


# ---------------------------------------------------------------------------
# Immutable per-turn state (Fix #1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnState:
    """Immutable snapshot of per-turn loop state.

    Each loop iteration produces a new ``TurnState`` instead of mutating
    local variables.  Use :meth:`replace` to derive a new instance with
    updated fields.
    """

    turn: int = 0
    turn_text: str = ""
    accumulated_text: str = ""
    accumulated_chars: int = 0
    tool_calls: tuple[ToolCallInfo, ...] = ()
    current_tool_name: str = ""
    current_tool_input_json: str = ""
    current_tool_raw: Any = None
    inside_tool_accumulation: bool = False
    emitted_keys: frozenset[str] = frozenset()
    messages: tuple[Message, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    has_attempted_reactive_compact: bool = False
    finish_reason: str = ""

    def replace(self, **changes: Any) -> TurnState:
        """Return a new ``TurnState`` with the given fields replaced."""
        return dataclasses.replace(self, **changes)

    def add_tool_call(self, tc: ToolCallInfo) -> TurnState:
        """Return a new state with *tc* appended to tool_calls."""
        return self.replace(tool_calls=(*self.tool_calls, tc))

    def add_emitted_key(self, key: str) -> TurnState:
        """Return a new state with *key* added to emitted_keys."""
        return self.replace(emitted_keys=self.emitted_keys | {key})


# ---------------------------------------------------------------------------
# Per-turn metrics (Fix #6 -- observability)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnMetrics:
    """Token usage and timing for a single turn."""

    turn: int
    input_tokens: int = 0
    output_tokens: int = 0
    tool_count: int = 0
    accumulated_chars: int = 0


# ---------------------------------------------------------------------------
# Error categorisation for retry logic
# ---------------------------------------------------------------------------


class ErrorCategory(enum.StrEnum):
    """Classification of errors for retry / fallback decisions.

    Backward-compat shim. The unified registry lives at
    `obscura.core.enums.error.ErrorCategory`; values here are byte-identical
    so wire format and identity-within-this-enum checks keep working.
    """

    TRANSIENT = "transient"
    MODEL_ERROR = "model_error"
    FATAL = "fatal"


def categorize_error(exc: Exception) -> ErrorCategory:
    """Classify *exc* into a retry-relevant category."""
    msg = str(exc).lower()

    status: int | None = None
    for attr in ("status_code", "status", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            status = val
            break

    # TRANSIENT
    if status == 429 or "rate_limit" in msg or "rate limit" in msg:
        return ErrorCategory.TRANSIENT
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ErrorCategory.TRANSIENT
    if isinstance(exc, (ConnectionError, OSError)):
        return ErrorCategory.TRANSIENT
    if "timeout" in msg or "timed out" in msg:
        return ErrorCategory.TRANSIENT
    if status is not None and 500 <= status < 600:
        return ErrorCategory.TRANSIENT
    if "connection" in msg and ("reset" in msg or "refused" in msg or "error" in msg):
        return ErrorCategory.TRANSIENT
    if "server error" in msg or "service unavailable" in msg:
        return ErrorCategory.TRANSIENT

    # MODEL_ERROR
    if (
        "context_length_exceeded" in msg
        or "prompt too long" in msg
        or "prompt is too long" in msg
    ):
        return ErrorCategory.MODEL_ERROR
    if "max_tokens" in msg or "output truncated" in msg or "maximum context" in msg:
        return ErrorCategory.MODEL_ERROR

    # FATAL
    if status in (401, 403):
        return ErrorCategory.FATAL
    if "unauthorized" in msg or "forbidden" in msg or "authentication" in msg:
        return ErrorCategory.FATAL
    if isinstance(exc, (ValueError, TypeError)):
        return ErrorCategory.FATAL

    return ErrorCategory.FATAL


# Type alias for confirmation callbacks.
# Receives a ToolCallInfo, returns True (approve) or False (deny).
ConfirmationCallback = Callable[[ToolCallInfo], Awaitable[bool] | bool]


class AgentLoop:
    """Iterative agent loop that drives tool-calling across multiple turns.

    The loop sends the initial prompt, collects the model response (streamed),
    detects any tool calls in the response, executes the tools locally, feeds
    the results back to the model as a new turn, and repeats.

    Parameters
    ----------
    backend:
        A started backend instance (CopilotBackend or ClaudeBackend).
    tool_registry:
        Registry of available tools. Tool handlers are called during the loop.
    max_turns:
        Maximum number of model turns before the loop stops.
    on_confirm:
        Optional async/sync callback invoked before each tool execution.
        Return ``True`` to approve, ``False`` to deny (tool result will
        be "Tool call denied by user").
    hooks:
        Optional :class:`HookRegistry`.  Before/after hooks fire on every
        event the loop emits.
    event_store:
        Optional :class:`EventStoreProtocol`.  When provided, every emitted
        event is persisted to durable storage.
    agent_name:
        Identifier for the agent (stored in the session record).
    """

    def __init__(
        self,
        backend: BackendProtocol | None,
        tool_registry: ToolRegistry,
        *,
        max_turns: int = 10,
        on_confirm: ConfirmationCallback | None = None,
        capability_token: Any | None = None,
        hooks: HookRegistry | None = None,
        event_store: EventStoreProtocol | None = None,
        agent_name: str = "agent_loop",
        tool_allowlist: list[str] | None = None,
        auto_complete: bool = True,
        backend_name: str = "",
        model_name: str = "",
        context_budget: int = 0,
        turn_timeout_s: float | None = None,
        compiled_agent: Any | None = None,
        tool_output_level: str = "standard",
        tool_output_overrides: dict[str, str] | None = None,
        host_callbacks: dict[str, Any] | None = None,
    ) -> None:
        self._backend = backend
        self._tools = tool_registry
        self._max_turns = max_turns
        self._on_confirm = on_confirm
        self._capability_token = capability_token
        self._hooks = hooks
        self._event_store = event_store
        self._agent_name = agent_name
        self._tool_allowlist = tool_allowlist
        self._auto_complete = auto_complete
        self._backend_name = backend_name
        self._model_name = model_name
        self._context_budget = context_budget  # 0 = unlimited (chars)
        self._turn_timeout_s = (
            turn_timeout_s  # per-turn stream timeout (None = no limit)
        )
        self._accumulated_chars = 0
        self._compiled_agent = compiled_agent
        self._tool_output_level = tool_output_level
        self._arbiter_killed = False  # Set by Arbiter to force-stop the loop.
        self._arbiter_kill_reason = ""
        self._tool_output_overrides = tool_output_overrides or {}
        # Optional per-instance host callbacks (ObscuraClient passes this when
        # the caller wired callbacks via the host_callbacks dict pattern). If
        # unset (the common case on this branch), tools fall back to the class
        # state on UI / Session.
        self._host_callbacks: dict[str, Any] = host_callbacks or {}

        # Apply compiled agent settings if provided
        if compiled_agent is not None:
            self._apply_compiled_agent(compiled_agent)

        # Track repeated NOT_FOUND failures across turns to break retry loops
        self._not_found_counts: dict[str, int] = {}

        # Pause / mid-run input state
        self._should_pause = False
        self._user_input_queue: asyncio.Queue[str] = asyncio.Queue()

        # Predictive tool calling — speculative prefetch of read-only tools
        self._predictive_enabled = os.environ.get(
            "OBSCURA_PREDICTIVE_TOOLS", "1"
        ).strip() not in ("0", "false", "no")
        self._predictive_cache = PredictiveToolCache()
        self._predictor: ToolPredictor | None = None

        # Conversation history reference for tools that need to inspect
        # or mutate it (e.g. history_snip). Updated each turn from the
        # kwargs["messages"] list — None when the backend manages history
        # opaquely (e.g. Claude SDK session state).
        self._current_messages: list[Any] | None = None
        self._current_session_id: str | None = None
        self._current_user: Any = None

        # Hallucination auto-correction (output_quality):
        #   _this_turn_successful_tools tracks tool calls that returned
        #     status="ok" during the current turn. Reset at TURN_START.
        #   _pending_correction holds a corrective message built when
        #     the scanner detects hallucination + recent successful tool;
        #     consumed at the top of the next turn iteration.
        self._this_turn_successful_tools: list[Any] = []
        self._pending_correction: str | None = None

    # ------------------------------------------------------------------
    # Compiled agent application
    # ------------------------------------------------------------------

    def _apply_compiled_agent(self, agent: Any) -> None:
        """Apply settings from a CompiledAgent to this loop.

        Reads tool_routing, env manifest, and MCP server configs from the
        compiled agent and wires them into the loop's state.
        """
        # Apply tool routing config
        if hasattr(agent, "tool_routing") and agent.tool_routing is not None:
            try:
                from obscura.core.tool_router import ToolRouter
                from obscura.core.tool_score_index import ToolScoreIndex

                router = ToolRouter(
                    config=agent.tool_routing,
                    score_index=ToolScoreIndex(),
                    backend=self._backend_name or "copilot",
                )
                if isinstance(self._backend, ToolRouterCapable):
                    self._backend.set_tool_router(router)
                logger.info("Applied tool routing from compiled agent")
            except Exception:
                logger.debug("Could not apply tool routing", exc_info=True)

        # Apply tool allowlist from compiled agent
        if hasattr(agent, "tool_allowlist") and agent.tool_allowlist:
            self._tool_allowlist = list(agent.tool_allowlist)

        # Apply max_turns from compiled agent
        if hasattr(agent, "max_turns") and agent.max_turns is not None:
            self._max_turns = agent.max_turns

        # Apply agent name
        if hasattr(agent, "name") and agent.name:
            self._agent_name = agent.name

        # Run preflight validation if env manifest is present
        if hasattr(agent, "env") and agent.env is not None:
            try:
                from obscura.core.preflight import PreflightValidator

                validator = PreflightValidator()
                result = validator.validate(agent)
                if result.passed:
                    logger.info(
                        "Preflight passed for agent '%s' (%d checks)",
                        agent.name,
                        len(result.checks),
                    )
                    self._preflight_result = result
                else:
                    logger.warning(
                        "Preflight failed for agent '%s': %s",
                        agent.name,
                        "; ".join(c.message for c in result.errors),
                    )
                    self._preflight_result = result
            except Exception:
                logger.debug("Preflight validation failed", exc_info=True)

    # ------------------------------------------------------------------
    # Pause / mid-run input public API
    # ------------------------------------------------------------------

    def request_pause(self) -> None:
        """Request the loop to pause at the next turn boundary.

        The current turn will complete normally.  After emitting
        TURN_COMPLETE the loop emits SESSION_PAUSED and returns.
        Thread-safe.
        """
        self._should_pause = True

    def inject_user_input(self, text: str) -> None:
        """Queue a user message to inject at the next turn boundary.

        The text becomes the prompt for the next model turn and a
        USER_INPUT event is emitted.  Thread-safe.
        """
        self._user_input_queue.put_nowait(text)

    def arbiter_kill(self, reason: str = "") -> None:
        """Signal the Arbiter to mechanically stop this loop.

        The loop will emit an AGENT_DONE event with the kill reason
        and return on the next turn boundary.  This is **not** prompt
        injection — the loop terminates because code says stop.
        Thread-safe.
        """
        self._arbiter_killed = True
        self._arbiter_kill_reason = reason or "Killed by Arbiter"

    @property
    def max_turns(self) -> int:
        """Read-only max_turns (testing/observability)."""
        return self._max_turns

    # ------------------------------------------------------------------
    # Event emission (hooks + persistence)
    # ------------------------------------------------------------------

    async def _emit(
        self,
        event: AgentEvent,
        session_id: str | None,
    ) -> AgentEvent | None:
        """Run before-hooks -> persist -> return event (or None if suppressed).

        After-hooks run separately via :meth:`_post_emit`.
        """
        current = event

        # Before-hooks
        if self._hooks is not None:
            result = await self._hooks.run_before(current)
            if result is None:
                return None
            current = result

        # Persist
        if self._event_store is not None and session_id is not None:
            await self._event_store.append(session_id, current)

        return current

    async def _post_emit(self, event: AgentEvent) -> None:
        """Run after-hooks for an already-yielded event."""
        if self._hooks is not None:
            await self._hooks.run_after(event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        initial_messages: list[Message] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AgentEvent]:
        """Run the agent loop, yielding events as they occur.

        Parameters
        ----------
        prompt:
            The initial user prompt.
        session_id:
            Optional durable session ID.  When provided (and an event store
            is configured), events are persisted and the session can be
            resumed later.  If ``None``, a transient ID is generated and
            nothing is persisted.

        Yields
        ------
        AgentEvent
            TURN_START, TEXT_DELTA, THINKING_DELTA, TOOL_CALL, TOOL_RESULT,
            TURN_COMPLETE, and finally AGENT_DONE (or ERROR).
        """
        # Reset pause state and per-run counters for this run
        self._should_pause = False
        self._not_found_counts.clear()

        # Create durable session if store is wired
        sid = session_id
        if self._event_store is not None and sid is not None:
            existing = await self._event_store.get_session(sid)
            if existing is None:
                await self._event_store.create_session(
                    sid,
                    self._agent_name,
                    backend=self._backend_name,
                    model=self._model_name,
                )

        # Emit AGENT_START
        start_event = AgentEvent(
            kind=AgentEventKind.AGENT_START,
            text=self._agent_name,
        )
        emitted = await self._emit(start_event, sid)
        if emitted is not None:
            yield emitted
            await self._post_emit(emitted)

        # Emit PREFLIGHT_PASS or PREFLIGHT_FAIL if preflight was run
        preflight_result = getattr(self, "_preflight_result", None)
        if preflight_result is not None:
            pf_kind = (
                AgentEventKind.PREFLIGHT_PASS
                if preflight_result.passed
                else AgentEventKind.PREFLIGHT_FAIL
            )
            pf_event = AgentEvent(
                kind=pf_kind,
                text="; ".join(c.message for c in preflight_result.checks),
            )
            emitted = await self._emit(pf_event, sid)
            if emitted is not None:
                yield emitted
                await self._post_emit(emitted)

            # Abort if preflight failed
            if not preflight_result.passed:
                err_event = AgentEvent(
                    kind=AgentEventKind.ERROR,
                    text=f"Preflight failed: {'; '.join(c.message for c in preflight_result.errors)}",
                )
                emitted = await self._emit(err_event, sid)
                if emitted is not None:
                    yield emitted
                    await self._post_emit(emitted)
                return

        try:
            async for event in self._run_inner(
                prompt, sid, 0, "", kwargs, initial_messages
            ):
                yield event
        finally:
            # Emit AGENT_STOP
            stop_event = AgentEvent(
                kind=AgentEventKind.AGENT_STOP,
                text=self._agent_name,
            )
            emitted = await self._emit(stop_event, sid)
            if emitted is not None:
                yield emitted
                await self._post_emit(emitted)

    async def resume(
        self,
        session_id: str,
        *,
        prompt: str = "",
        **kwargs: Any,
    ) -> AsyncIterator[AgentEvent]:
        """Resume a paused session from the event store.

        Parameters
        ----------
        session_id:
            The session to resume.  Must exist and have status PAUSED.
        prompt:
            Optional override prompt.  If empty, the reconstructed prompt
            from the last run is used.

        Raises
        ------
        RuntimeError
            If no event store is configured.
        ValueError
            If the session does not exist or is not paused.
        """
        if self._event_store is None:
            raise RuntimeError("Cannot resume without an event store")

        session = await self._event_store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id!r} not found")

        if session.status != SessionStatus.PAUSED:
            raise ValueError(
                f"Session {session_id!r} is {session.status.value}, not paused"
            )

        # Reconstruct state from persisted events
        events = await self._event_store.get_events(session_id)
        turn, acc_text, messages, last_prompt = AgentLoop.reconstruct_state(events)

        # Transition back to RUNNING
        await self._event_store.update_status(session_id, SessionStatus.RUNNING)

        # Reset pause flag
        self._should_pause = False

        resume_prompt = prompt if prompt else last_prompt
        resume_kwargs: dict[str, Any] = dict(kwargs)
        if messages:
            resume_kwargs["messages"] = messages

        async for event in self._run_inner(
            resume_prompt,
            session_id,
            turn,
            acc_text,
            resume_kwargs,
        ):
            yield event

    # ------------------------------------------------------------------
    # Inner loop (shared by run + resume)
    # ------------------------------------------------------------------

    async def _call_stream(
        self,
        prompt: str,
        kwargs: dict[str, Any],
    ) -> AsyncIterator[StreamChunk]:
        """Call backend.stream with kwargs, isolating type-unsafe spread."""
        if self._backend is None:
            raise RuntimeError("No backend configured")
        async for chunk in self._backend.stream(prompt, **kwargs):
            yield chunk

    async def _run_inner(
        self,
        prompt: str,
        session_id: str | None,
        start_turn: int,
        accumulated_text: str,
        stream_kwargs: dict[str, Any],
        initial_messages: list[Message] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Core loop body shared by :meth:`run` and :meth:`resume`.

        Uses an immutable :class:`TurnState` -- each iteration produces a
        new state instead of mutating locals.
        """
        state = TurnState(
            turn=start_turn,
            accumulated_text=accumulated_text,
        )
        current_prompt: str = prompt
        kwargs: dict[str, Any] = stream_kwargs
        # Expose mutable conversation history to ToolContext so tools like
        # history_snip can inspect / mutate it. Backends that don't pass
        # messages through kwargs (e.g. Claude SDK with native sessions)
        # will leave this as None and history-mutating tools will return
        # a clean "no_history" error.
        msgs = kwargs.get("messages")
        if isinstance(msgs, list):
            self._current_messages = msgs
        self._current_session_id = session_id
        _prev_event: AgentEvent | None = None
        _retry_count: int = 0
        _max_retries: int = 3
        _stop_hook_continuations: int = 0
        _max_tokens_retries: int = 0
        # Dedup cache shared across retries of the same turn. Tools that
        # already executed (with real side effects) get replayed from this
        # cache instead of re-executed when the stream is retried after a
        # timeout / transient error. Cleared at the top of the next fresh
        # turn (where _retry_count == 0).
        _seen_calls_for_retry: dict[str, ToolResultEnvelope] = {}

        while state.turn < self._max_turns:
            # Clear the cross-retry dedup cache at the top of each fresh
            # turn (retries keep _retry_count > 0 and preserve the cache).
            if _retry_count == 0:
                _seen_calls_for_retry.clear()
            # Arbiter kill: mechanical stop — no prompt injection, loop ends.
            if self._arbiter_killed:
                kill_event = AgentEvent(
                    kind=AgentEventKind.AGENT_DONE,
                    turn=state.turn,
                    text=f"[Arbiter KILL] {self._arbiter_kill_reason}",
                )
                await self._emit(kill_event, session_id)
                break

            state = state.replace(turn=state.turn + 1)

            # Fresh per-turn state (carry over accumulated_text/chars/tokens)
            state = state.replace(
                turn_text="",
                tool_calls=(),
                current_tool_name="",
                current_tool_input_json="",
                current_tool_raw=None,
                inside_tool_accumulation=False,
                emitted_keys=frozenset(),
                input_tokens=0,
                output_tokens=0,
                finish_reason="",
            )
            # Reset per-turn successful-tool tracking; persists only inside one turn.
            self._this_turn_successful_tools = []

            # Inject any pending hallucination correction into the next prompt.
            # Set when the prior turn's text-quality scan caught the model
            # narrating failure that contradicted a successful tool call —
            # see ``obscura/core/output_quality.py``. Emitted as a
            # ``CORRECTION_INJECTED`` event so the REPL can surface that a
            # course-correction was applied.
            if self._pending_correction:
                correction: str = self._pending_correction
                self._pending_correction = None
                # Pyright loses str inference here through the async generator
                # boundary; see comment on the _call_stream call below.
                current_prompt = (  # pyright: ignore[reportUnknownVariableType]
                    correction + "\n\n" + current_prompt
                    if current_prompt
                    else correction
                )
                correction_event = AgentEvent(
                    kind=AgentEventKind.CORRECTION_INJECTED,
                    turn=state.turn,
                    text=correction,
                )
                emitted = await self._emit(correction_event, session_id)
                if emitted is not None:
                    yield emitted

            # Run post-hook for the previous event before emitting next
            if _prev_event is not None:
                await self._post_emit(_prev_event)
                _prev_event = None

            turn_start = AgentEvent(kind=AgentEventKind.TURN_START, turn=state.turn)
            emitted = await self._emit(turn_start, session_id)
            if emitted is not None:
                yield emitted
                _prev_event = emitted

            # Mutable list used by _flush_pending_tool (passed by reference)
            _tool_calls_mut: list[ToolCallInfo] = []
            _emitted_keys_mut: set[str] = set()

            # Streaming tool executor: starts tools as they arrive from
            # the model stream, not after the full response completes.
            executor = StreamingToolExecutor(
                tool_lookup=self._tools.get,
                execute_tool=self._execute_single_tool,
            )
            # Share the cross-retry dedup cache. If this turn is a retry
            # after a timeout / transient stream error, any tool that
            # completed on a prior attempt will be returned from cache
            # instead of re-executed — preventing duplicate side effects
            # like double `git commit`.
            executor.seen_calls = _seen_calls_for_retry

            # Predictive tool calling: speculatively prefetch read-only
            # tools based on the model's text output before it even emits
            # a tool_use block.
            if self._predictive_enabled:
                self._predictive_cache.clear()
                self._predictor = ToolPredictor(
                    tool_registry={spec.name: spec for spec in self._tools.all()},
                )

            def _feed_new_tools_to_executor() -> None:
                """Hand any newly-flushed tool calls to the executor."""
                fed = len(executor.order)
                for tc in _tool_calls_mut[fed:]:
                    executor.add_tool(tc)

            try:
                async with asyncio.timeout(self._turn_timeout_s):
                    # NB: pyright reports current_prompt and kwargs as
                    # ``X | Unknown`` here — pyright loses inference across
                    # the async-generator iteration boundary in this loop.
                    # The runtime types are guaranteed by the explicit
                    # annotations above (``current_prompt: str``,
                    # ``kwargs: dict[str, Any]``).
                    async for chunk in self._call_stream(
                        current_prompt,  # pyright: ignore[reportUnknownArgumentType]
                        kwargs,  # pyright: ignore[reportUnknownArgumentType]
                    ):
                        # Fix #4: suppress text only inside tool accumulation.
                        # Text between TOOL_USE_START and TOOL_USE_END is dropped
                        # (hallucinated tool output).  Text after TOOL_USE_END but
                        # before the next TOOL_USE_START is kept (legitimate
                        # interleaved explanation).
                        if (
                            chunk.kind == ChunkKind.TEXT_DELTA
                            and state.inside_tool_accumulation
                        ):
                            continue

                        event = self._map_chunk(chunk, state.turn)
                        if event is not None:
                            # Run post-hook for previous, then emit new
                            if _prev_event is not None:
                                await self._post_emit(_prev_event)
                                _prev_event = None
                            emitted = await self._emit(event, session_id)
                            if emitted is not None:
                                yield emitted
                                _prev_event = emitted

                        # Accumulate text
                        if chunk.kind == ChunkKind.TEXT_DELTA:
                            state = state.replace(
                                turn_text=state.turn_text + chunk.text
                            )
                            # Feed text to predictive tool caller
                            if self._predictor is not None:
                                self._predictor.feed(chunk.text)
                                self._fire_predictions()

                        # Collect tool calls
                        if chunk.kind == ChunkKind.TOOL_USE_START:
                            state = state.replace(inside_tool_accumulation=True)
                            # Flush previous tool if any (fallback for backends
                            # that don't emit TOOL_USE_END)
                            if state.current_tool_name:
                                _to_yield, _prev_event = await self._flush_pending_tool(
                                    state.current_tool_name,
                                    state.current_tool_input_json,
                                    state.current_tool_raw,
                                    turn=state.turn,
                                    emitted_keys=_emitted_keys_mut,
                                    tool_calls=_tool_calls_mut,
                                    prev_event=_prev_event,
                                    session_id=session_id,
                                )
                                if _to_yield is not None:
                                    yield _to_yield
                                _feed_new_tools_to_executor()
                            state = state.replace(
                                current_tool_name=chunk.tool_name,
                                current_tool_input_json="",
                                current_tool_raw=chunk.raw,
                            )

                        if chunk.kind == ChunkKind.TOOL_USE_DELTA:
                            state = state.replace(
                                current_tool_input_json=(
                                    state.current_tool_input_json
                                    + chunk.tool_input_delta
                                ),
                            )

                        # Fix #2: SDK tool results are logged but not collected
                        # for re-use.  All tools are executed locally.
                        if chunk.kind == ChunkKind.TOOL_RESULT:
                            logger.debug(
                                "SDK tool result received (ignored): %s",
                                chunk.text[:120] if chunk.text else "",
                            )

                        # TOOL_USE_END -- flush accumulated tool immediately
                        if chunk.kind == ChunkKind.TOOL_USE_END:
                            if state.current_tool_name:
                                _to_yield, _prev_event = await self._flush_pending_tool(
                                    state.current_tool_name,
                                    state.current_tool_input_json,
                                    state.current_tool_raw,
                                    turn=state.turn,
                                    emitted_keys=_emitted_keys_mut,
                                    tool_calls=_tool_calls_mut,
                                    prev_event=_prev_event,
                                    session_id=session_id,
                                )
                                if _to_yield is not None:
                                    yield _to_yield
                                # Feed newly flushed tool to executor immediately
                                _feed_new_tools_to_executor()
                                state = state.replace(
                                    current_tool_name="",
                                    current_tool_input_json="",
                                    current_tool_raw=None,
                                    inside_tool_accumulation=False,
                                )

                        # Fix #6: extract per-turn token usage from DONE metadata
                        if chunk.kind == ChunkKind.DONE and chunk.metadata is not None:
                            usage = chunk.metadata.usage
                            finish_reason = (
                                getattr(chunk.metadata, "finish_reason", "") or ""
                            )
                            if usage is not None:
                                state = state.replace(
                                    finish_reason=finish_reason,
                                    input_tokens=usage.get("input_tokens", 0),
                                    output_tokens=usage.get("output_tokens", 0),
                                )
                            else:
                                state = state.replace(finish_reason=finish_reason)

                # Flush last tool call (fallback if no TOOL_USE_END received)
                if state.current_tool_name:
                    _to_yield, _prev_event = await self._flush_pending_tool(
                        state.current_tool_name,
                        state.current_tool_input_json,
                        state.current_tool_raw,
                        turn=state.turn,
                        emitted_keys=_emitted_keys_mut,
                        tool_calls=_tool_calls_mut,
                        prev_event=_prev_event,
                        session_id=session_id,
                    )
                    if _to_yield is not None:
                        yield _to_yield
                    _feed_new_tools_to_executor()

                # Mark executor closed -- no more tools will arrive
                executor.close()

                # -- Max output tokens recovery --------------------------------
                # If the model's response was truncated due to max_output_tokens,
                # discard the (likely broken) output and retry with a higher limit.
                if state.finish_reason == "max_tokens":
                    if _max_tokens_retries < MAX_OUTPUT_TOKENS_RETRIES:
                        _max_tokens_retries += 1

                        # Cancel any in-flight tools from the truncated response
                        executor.abort_event.set()
                        for task in list(executor.in_flight.values()):
                            task.cancel()

                        # Escalate max_tokens
                        current_max_tokens = kwargs.get(
                            "max_tokens", DEFAULT_MAX_TOKENS
                        )
                        escalated = min(current_max_tokens * 2, ESCALATED_MAX_TOKENS)
                        kwargs["max_tokens"] = escalated

                        logger.warning(
                            "Response truncated (max_tokens). Retrying with "
                            "%d tokens (attempt %d/%d)",
                            escalated,
                            _max_tokens_retries,
                            MAX_OUTPUT_TOKENS_RETRIES,
                        )

                        # Discard the truncated response — retry the turn
                        state = state.replace(
                            turn=state.turn - 1,
                            turn_text="",
                            tool_calls=(),
                            finish_reason="",
                        )
                        continue  # Retry the turn with higher limit
                    else:
                        logger.error(
                            "Max output tokens retries exhausted (%d attempts)",
                            MAX_OUTPUT_TOKENS_RETRIES,
                        )
                        # Fall through to normal processing with whatever we got
                else:
                    # Successful (non-truncated) turn: reset retry counter
                    _max_tokens_retries = 0

            except TimeoutError:
                # Per-turn stream timeout — abort in-flight tools and
                # surface as a retryable TRANSIENT error so the existing
                # backoff logic can kick in.
                executor.close()
                executor.abort_event.set()
                for task in list(executor.in_flight.values()):
                    task.cancel()

                if _retry_count < _max_retries:
                    _retry_count += 1
                    backoff = 2 ** (_retry_count - 1)
                    logger.warning(
                        "Turn %d timed out after %ss (attempt %d/%d), retrying in %ds",
                        state.turn,
                        self._turn_timeout_s,
                        _retry_count,
                        _max_retries,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    state = state.replace(turn=state.turn - 1)
                    continue

                # Exhausted retries — emit error and stop
                err_event = AgentEvent(
                    kind=AgentEventKind.ERROR,
                    text=f"Turn timed out after {self._turn_timeout_s}s "
                    f"({_max_retries} retries exhausted)",
                    turn=state.turn,
                )
                if _prev_event is not None:
                    await self._post_emit(_prev_event)
                emitted = await self._emit(err_event, session_id)
                if emitted is not None:
                    yield emitted
                    await self._post_emit(emitted)
                if (
                    self._auto_complete
                    and self._event_store is not None
                    and session_id is not None
                ):
                    try:
                        await self._event_store.update_status(
                            session_id, SessionStatus.FAILED
                        )
                    except Exception:
                        logger.debug("Failed to mark session failed", exc_info=True)
                return

            except Exception as exc:
                # Close executor and cancel in-flight tools on stream error
                executor.close()
                executor.abort_event.set()
                for task in list(executor.in_flight.values()):
                    task.cancel()

                category = categorize_error(exc)

                # -- TRANSIENT: retry with exponential backoff ---------------
                if category is ErrorCategory.TRANSIENT and _retry_count < _max_retries:
                    _retry_count += 1
                    backoff = 2 ** (_retry_count - 1)  # 1s, 2s, 4s
                    logger.warning(
                        "Transient error on turn %d (attempt %d/%d), "
                        "retrying in %ds: %s",
                        state.turn,
                        _retry_count,
                        _max_retries,
                        backoff,
                        exc,
                    )
                    await asyncio.sleep(backoff)
                    state = state.replace(turn=state.turn - 1)
                    continue

                # -- MODEL_ERROR: attempt recovery ---------------------------
                if category is ErrorCategory.MODEL_ERROR:
                    exc_msg = str(exc).lower()

                    # Reactive mid-turn compaction for context-too-long errors
                    if (
                        self._is_context_too_long(exc)
                        and not state.has_attempted_reactive_compact
                    ):
                        result = await self._reactive_compact(
                            kwargs,  # pyright: ignore[reportUnknownArgumentType]
                            state,
                        )
                        if result is not None:
                            kwargs, state = result
                            # Emit compaction event
                            compact_event = AgentEvent(
                                kind=AgentEventKind.CONTEXT_COMPACT,
                                text="Reactive mid-turn compaction triggered",
                                turn=state.turn,
                            )
                            if _prev_event is not None:
                                await self._post_emit(_prev_event)
                                _prev_event = None
                            emitted = await self._emit(compact_event, session_id)
                            if emitted is not None:
                                yield emitted
                                _prev_event = emitted
                            # Don't increment retry count — compaction is
                            # recovery, not retry.  Rewind turn so the while
                            # loop re-enters at the same turn number.
                            state = state.replace(turn=state.turn - 1)
                            continue

                    if (
                        "max_tokens" in exc_msg or "output truncated" in exc_msg
                    ) and _retry_count < 1:
                        _retry_count += 1
                        logger.warning(
                            "Output truncated on turn %d, retrying",
                            state.turn,
                        )
                        state = state.replace(turn=state.turn - 1)
                        continue

                # -- FATAL (or exhausted retries) ----------------------------
                err_event = AgentEvent(
                    kind=AgentEventKind.ERROR,
                    text=str(exc),
                    turn=state.turn,
                    raw=exc,
                )
                if _prev_event is not None:
                    await self._post_emit(_prev_event)
                emitted = await self._emit(err_event, session_id)
                if emitted is not None:
                    yield emitted
                    await self._post_emit(emitted)
                # Mark session failed
                if (
                    self._auto_complete
                    and self._event_store is not None
                    and session_id is not None
                ):
                    try:
                        await self._event_store.update_status(
                            session_id, SessionStatus.FAILED
                        )
                    except Exception:
                        logger.debug("Failed to mark session failed", exc_info=True)
                return
            else:
                # Successful turn: reset retry counter. The dedup cache is
                # cleared at the top of the NEXT iteration (once retries
                # can't fire for this turn), not here — a scheduled tool
                # task may still need to hit the cache before wait_for_all.
                _retry_count = 0

            # Sync mutable tool_calls back into immutable state
            state = state.replace(
                tool_calls=tuple(_tool_calls_mut),
                emitted_keys=frozenset(_emitted_keys_mut),
                accumulated_text=state.accumulated_text + state.turn_text,
            )

            # Scan accumulated turn text for known hallucination patterns
            # (model inventing UX flows like "click Allow" or
            # "/allowed-tools" that don't exist in obscura). Logs at
            # WARNING so violations surface in default-level logs without
            # changing the output the user sees. When a hallucination
            # contradicts a successful tool call from the same turn, also
            # build a corrective message that the next turn will inject.
            try:
                from obscura.core.output_quality import (
                    build_correction_prompt,
                    log_violations,
                    scan_text,
                )

                violations = scan_text(state.turn_text)
                if violations:
                    log_violations(violations, turn=state.turn)
                    if self._this_turn_successful_tools:
                        correction = build_correction_prompt(
                            violations,
                            self._this_turn_successful_tools,
                        )
                        if correction:
                            self._pending_correction = correction
            except Exception:
                # Quality scan must never break the turn — best-effort.
                logger.debug("suppressed exception in _run_inner", exc_info=True)

            # Emit TURN_COMPLETE
            if _prev_event is not None:
                await self._post_emit(_prev_event)
                _prev_event = None
            tc_event = AgentEvent(
                kind=AgentEventKind.TURN_COMPLETE, turn=state.turn, text=state.turn_text
            )
            emitted = await self._emit(tc_event, session_id)
            if emitted is not None:
                yield emitted
                _prev_event = emitted

            # Fix #6: emit TurnMetrics for observability
            metrics = TurnMetrics(
                turn=state.turn,
                input_tokens=state.input_tokens,
                output_tokens=state.output_tokens,
                tool_count=len(state.tool_calls),
                accumulated_chars=len(state.accumulated_text),
            )
            logger.info(
                "Turn %d metrics: in=%d out=%d tools=%d chars=%d",
                metrics.turn,
                metrics.input_tokens,
                metrics.output_tokens,
                metrics.tool_count,
                metrics.accumulated_chars,
            )

            # ----------------------------------------------------------
            # Turn boundary: check pause / user input before next turn
            # ----------------------------------------------------------

            # Pause check -- current turn completed, pause before next
            if self._should_pause:
                if _prev_event is not None:
                    await self._post_emit(_prev_event)
                    _prev_event = None
                pause_event = AgentEvent(
                    kind=AgentEventKind.SESSION_PAUSED,
                    turn=state.turn,
                    text="Session paused at turn boundary",
                )
                emitted = await self._emit(pause_event, session_id)
                if emitted is not None:
                    yield emitted
                    await self._post_emit(emitted)
                # Mark session paused
                if self._event_store is not None and session_id is not None:
                    try:
                        await self._event_store.update_status(
                            session_id, SessionStatus.PAUSED
                        )
                    except Exception:
                        logger.debug("Failed to mark session paused", exc_info=True)
                return

            # Mid-run user input -- drain queue, use as next prompt
            if not self._user_input_queue.empty():
                if _prev_event is not None:
                    await self._post_emit(_prev_event)
                    _prev_event = None
                user_text = self._user_input_queue.get_nowait()
                ui_event = AgentEvent(
                    kind=AgentEventKind.USER_INPUT,
                    turn=state.turn,
                    text=user_text,
                )
                emitted = await self._emit(ui_event, session_id)
                if emitted is not None:
                    yield emitted
                    _prev_event = emitted
                # Override the next prompt with the user's input
                current_prompt = user_text
                # Skip tool execution for this turn -- go straight to
                # next model call with the injected prompt.
                # Remove "messages" from kwargs so the backend doesn't
                # replay stale tool results.
                kwargs.pop("messages", None)
                continue

            # No tool calls -> model wants to stop.
            # Fire STOP_CHECK so before-hooks can intervene.
            #
            # Hook contract for STOP_CHECK before-hooks:
            #   - Return None → suppress the stop, continue with empty prompt
            #   - Return modified event with new .text → continue with that
            #     text as the next prompt
            #   - Return event unchanged → allow the stop (AGENT_DONE fires)
            if not state.tool_calls:
                if _prev_event is not None:
                    await self._post_emit(_prev_event)
                    _prev_event = None

                # Guard: force stop after too many hook continuations
                if _stop_hook_continuations >= MAX_STOP_HOOK_CONTINUATIONS:
                    logger.warning(
                        "Stop hook continuation limit (%d) reached, forcing stop",
                        MAX_STOP_HOOK_CONTINUATIONS,
                    )
                else:
                    # Fire STOP_CHECK — before-hooks can suppress or redirect
                    stop_event = AgentEvent(
                        kind=AgentEventKind.STOP_CHECK,
                        text=state.accumulated_text,
                        turn=state.turn,
                    )
                    result = await self._emit(stop_event, session_id)

                    if result is None:
                        # Hook suppressed the stop — continue with empty prompt
                        _stop_hook_continuations += 1
                        current_prompt = ""
                        continue

                    if result.text and result.text != state.accumulated_text:
                        # Hook provided a new prompt — continue with it
                        _stop_hook_continuations += 1
                        current_prompt = result.text
                        kwargs.pop("messages", None)
                        continue

                # No hook intervention (or guard hit) — stop normally
                done_event = AgentEvent(
                    kind=AgentEventKind.AGENT_DONE,
                    turn=state.turn,
                    text=state.accumulated_text,
                )
                emitted = await self._emit(done_event, session_id)
                if emitted is not None:
                    yield emitted
                    await self._post_emit(emitted)
                # Mark session completed
                if (
                    self._auto_complete
                    and self._event_store is not None
                    and session_id is not None
                ):
                    try:
                        await self._event_store.update_status(
                            session_id, SessionStatus.COMPLETED
                        )
                    except Exception:
                        logger.debug("Failed to mark session completed", exc_info=True)
                return

            # Fix #2: Always execute tools locally -- backend is a pure
            # LLM interface.  No SDK/local execution fork.
            # Tools were already dispatched to the StreamingToolExecutor
            # during streaming.  Wait for any remaining in-flight tools.
            tool_calls_list = executor.get_tool_calls_in_order()
            if executor.has_tools:
                tool_results = await executor.wait_for_all()
            else:
                # Fallback: if no tools were fed to executor (shouldn't
                # happen when tool_calls is non-empty, but be defensive)
                tool_calls_list = list(state.tool_calls)
                tool_results = await self._execute_tools(tool_calls_list)

            # Yield tool result events (and TOOL_CALL_FAILURE for errors)
            for result in tool_results:
                if _prev_event is not None:
                    await self._post_emit(_prev_event)
                    _prev_event = None
                tr_event = AgentEvent(
                    kind=AgentEventKind.TOOL_RESULT,
                    tool_name=result.tool,
                    tool_use_id=result.tool_use_id,
                    tool_result=self._render_tool_result_text(result),
                    is_error=result.status == "error",
                    turn=state.turn,
                    raw=result,
                )
                emitted = await self._emit(tr_event, session_id)
                if emitted is not None:
                    yield emitted
                    _prev_event = emitted

                # Emit TOOL_CALL_FAILURE for error results so hooks can react
                if result.status == "error":
                    if _prev_event is not None:
                        await self._post_emit(_prev_event)
                        _prev_event = None
                    fail_event = AgentEvent(
                        kind=AgentEventKind.TOOL_CALL_FAILURE,
                        tool_name=result.tool,
                        tool_use_id=result.tool_use_id,
                        tool_result=result.error.message
                        if result.error
                        else "Tool error",
                        is_error=True,
                        turn=state.turn,
                        raw=result,
                    )
                    emitted = await self._emit(fail_event, session_id)
                    if emitted is not None:
                        yield emitted
                        _prev_event = emitted

            # Fix #5: Only use structured messages -- no dual prompt format.
            # Build structured messages and use an empty continuation prompt.
            structured = self._build_structured_tool_messages(
                tool_calls_list,
                tool_results,
                state.turn_text,
            )
            current_prompt = ""

            # Log predictive cache stats and reset for next turn
            if self._predictive_enabled:
                stats = self._predictive_cache.stats
                if stats["hits"] or stats["misses"]:
                    logger.info(
                        "Predictive tools: %d hits, %d misses, %d still pending",
                        stats["hits"],
                        stats["misses"],
                        stats["pending"],
                    )

            # Pass structured messages via kwargs so backends can
            # persist full tool call/result history.  Merge rather
            # than replace so callers' kwargs (e.g. tool_choice) survive.
            kwargs = {**kwargs, "messages": structured}

            # Context budget: track accumulated chars and compact when
            # the internal message list grows too large.
            # Fix #6: prefer token-based budget when token usage is available.
            if self._context_budget > 0:
                if state.input_tokens > 0 or state.output_tokens > 0:
                    # Token-based budget check (tokens are roughly 4 chars)
                    total_tokens = state.input_tokens + state.output_tokens
                    budget_tokens = self._context_budget // 4
                    if total_tokens > budget_tokens:
                        kwargs, dropped, freed = self._compact_messages(kwargs)
                        compact_event = AgentEvent(
                            kind=AgentEventKind.CONTEXT_COMPACT,
                            turn=state.turn,
                            text=(
                                f"Compacted {dropped} tool turns "
                                f"(~{total_tokens:,} tokens, {freed:,} chars freed)"
                            ),
                        )
                        if _prev_event is not None:
                            await self._post_emit(_prev_event)
                            _prev_event = None
                        emitted = await self._emit(compact_event, session_id)
                        if emitted is not None:
                            yield emitted
                            _prev_event = emitted
                else:
                    # Legacy char-based compaction fallback
                    turn_chars = sum(
                        len(self._render_tool_result_text(r)) for r in tool_results
                    ) + len(state.turn_text)
                    self._accumulated_chars += turn_chars
                    if self._accumulated_chars > self._context_budget:
                        kwargs, dropped, freed = self._compact_messages(kwargs)
                        compact_event = AgentEvent(
                            kind=AgentEventKind.CONTEXT_COMPACT,
                            turn=state.turn,
                            text=(
                                f"Compacted {dropped} tool turns "
                                f"({freed:,} chars freed)"
                            ),
                        )
                        if _prev_event is not None:
                            await self._post_emit(_prev_event)
                            _prev_event = None
                        emitted = await self._emit(compact_event, session_id)
                        if emitted is not None:
                            yield emitted
                            _prev_event = emitted

        # Hit max turns
        if _prev_event is not None:
            await self._post_emit(_prev_event)
        done_event = AgentEvent(
            kind=AgentEventKind.AGENT_DONE,
            turn=state.turn,
            text=state.accumulated_text,
        )
        emitted = await self._emit(done_event, session_id)
        if emitted is not None:
            yield emitted
            await self._post_emit(emitted)
        # Mark session completed
        if (
            self._auto_complete
            and self._event_store is not None
            and session_id is not None
        ):
            try:
                await self._event_store.update_status(
                    session_id, SessionStatus.COMPLETED
                )
            except Exception:
                logger.debug("Failed to mark session completed", exc_info=True)

    # ------------------------------------------------------------------
    # Convenience: run and collect final text
    # ------------------------------------------------------------------

    async def run_to_completion(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Run the loop and return the concatenated text output."""
        text_parts: list[str] = []
        async for event in self.run(prompt, session_id=session_id, **kwargs):
            if event.kind == AgentEventKind.TEXT_DELTA:
                text_parts.append(event.text)
        return "".join(text_parts)

    # ------------------------------------------------------------------
    # Tool-call flush helper (shared by TOOL_USE_START/END/fallback)
    # ------------------------------------------------------------------

    async def _flush_pending_tool(
        self,
        name: str,
        input_json: str,
        raw: Any,
        *,
        turn: int,
        emitted_keys: set[str],
        tool_calls: list[ToolCallInfo],
        prev_event: AgentEvent | None,
        session_id: str | None,
    ) -> tuple[AgentEvent | None, AgentEvent | None]:
        """Parse, deduplicate, and emit a tool call event.

        Returns ``(event_to_yield, new_prev_event)``.  The caller must
        ``yield event_to_yield`` if it is not ``None``.
        """
        tc = self._parse_tool_call(name, input_json, raw)

        # Special-case: expand multi-tool wrapper payloads (e.g. "parallel"
        # or other orchestrators that carry a list of nested tool_uses).
        multi_wrappers = {"parallel", "multi_tool", "multi_tool_use"}
        if tc.name in multi_wrappers:
            inner_raw: Any = tc.input.get("tool_uses") or tc.input.get("toolUses")
            inner: list[Any] = (
                cast(list[Any], inner_raw) if isinstance(inner_raw, list) else []
            )
            last_emitted: AgentEvent | None = None
            for use_any in inner:
                if not isinstance(use_any, dict):
                    continue
                use = cast(dict[str, Any], use_any)
                recipient_raw: Any = use.get("recipient_name") or use.get("recipient")
                params_raw: Any = use.get("parameters") or use.get("args")
                if not recipient_raw or not isinstance(recipient_raw, str):
                    continue
                recipient: str = recipient_raw
                params: dict[str, Any] = (
                    cast(dict[str, Any], params_raw)
                    if isinstance(params_raw, dict)
                    else {}
                )
                # Normalize dotted provider prefixes (skip mcp__-prefixed
                # names — see _parse_tool_call for rationale).
                if "." in recipient and not recipient.startswith("mcp__"):
                    recipient = recipient.rsplit(".", maxsplit=1)[-1]

                # Skip inner recipients that are not registered to avoid
                # emitting error results for orchestrator wrapper entries.
                if self._tools.get(recipient) is None:
                    continue

                inner_tc = ToolCallInfo(
                    tool_use_id=f"tool_{uuid.uuid4().hex[:12]}",
                    name=recipient,
                    input=params,
                    raw=use,
                )
                dedup_key = (
                    f"{inner_tc.name}|{json.dumps(inner_tc.input, sort_keys=True)}"
                )
                if dedup_key in emitted_keys:
                    continue
                emitted_keys.add(dedup_key)
                tool_calls.append(inner_tc)

                tc_ev = AgentEvent(
                    kind=AgentEventKind.TOOL_CALL,
                    tool_name=inner_tc.name,
                    tool_input=inner_tc.input,
                    turn=turn,
                    raw=inner_tc.raw,
                )
                if prev_event is not None:
                    await self._post_emit(prev_event)
                    prev_event = None
                emitted = await self._emit(tc_ev, session_id)
                if emitted is not None:
                    last_emitted = emitted

            return last_emitted, last_emitted

        # Default single-tool flow
        dedup_key = f"{tc.name}|{json.dumps(tc.input, sort_keys=True)}"
        if dedup_key in emitted_keys:
            return None, prev_event
        emitted_keys.add(dedup_key)
        tool_calls.append(tc)
        tc_ev = AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            tool_name=tc.name,
            tool_input=tc.input,
            turn=turn,
            raw=tc.raw,
        )
        if prev_event is not None:
            await self._post_emit(prev_event)
        emitted = await self._emit(tc_ev, session_id)
        if emitted is not None:
            return emitted, emitted
        return None, None

    # ------------------------------------------------------------------
    # Predictive tool calling
    # ------------------------------------------------------------------

    def _fire_predictions(self) -> None:
        """Check the predictor for new predictions and prefetch them."""
        if self._predictor is None:
            return
        for pred in self._predictor.predict():
            if self._predictive_cache.has(pred.tool, pred.args):
                continue
            # Only prefetch if not already in cache
            seen: dict[str, ToolResultEnvelope] = {}
            tc = ToolCallInfo(
                tool_use_id=f"predict-{uuid.uuid4().hex[:8]}",
                name=pred.tool,
                input=dict(pred.args),
            )
            task = asyncio.create_task(
                self._execute_single_tool(tc, seen),
                name=f"prefetch-{pred.tool}",
            )
            self._predictive_cache.put(pred.tool, pred.args, task)
            logger.debug(
                "Predictive prefetch fired: %s(%s)",
                pred.tool,
                list(pred.args.keys()),
            )

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _partition_tool_calls(
        self,
        tool_calls: list[ToolCallInfo],
    ) -> list[tuple[bool, list[ToolCallInfo]]]:
        """Partition into ``(is_concurrent, [calls])`` batches.

        Consecutive concurrency-safe tools form one batch.
        Each non-concurrent tool is its own batch.
        """
        batches: list[tuple[bool, list[ToolCallInfo]]] = []
        for tc in tool_calls:
            spec = self._tools.get(tc.name)
            is_safe = spec is not None and spec.is_concurrency_safe()
            if batches and batches[-1][0] == is_safe:
                batches[-1][1].append(tc)
            else:
                batches.append((is_safe, [tc]))
        return batches

    async def _gather_with_limit(
        self,
        coros: list[Coroutine[Any, Any, ToolResultEnvelope]],
        limit: int,
    ) -> list[ToolResultEnvelope]:
        """Run coroutines concurrently with a semaphore-based limit."""
        semaphore = asyncio.Semaphore(limit)

        async def limited(
            coro: Coroutine[Any, Any, ToolResultEnvelope],
        ) -> ToolResultEnvelope:
            async with semaphore:
                return await coro

        return list(await asyncio.gather(*[limited(c) for c in coros]))

    def _read_host_callbacks(self) -> dict[str, Any]:
        """Snapshot host callbacks for splatting into ToolContext(...).

        Prefers per-instance values passed via ``host_callbacks=`` to
        ``__init__`` (the new pattern). Falls back to UI / Session class
        state for callbacks the caller did not supply, so legacy callers
        that wired callbacks via ``UI.set_ask_user_callback`` etc. keep
        working.
        """
        try:
            from obscura.tools.system import UI, Session

            defaults = {
                "ask_user_callback": UI.ask_user_callback,
                "user_interact_callback": UI.user_interact_callback,
                "permission_mode_callback": Session.permission_mode_callback,
                "plan_approval_callback": Session.plan_approval_callback,
            }
        except Exception:
            logger.debug("suppressed exception in _read_host_callbacks", exc_info=True)
            defaults = {}

        # Per-instance overrides win over class state.
        for k, v in self._host_callbacks.items():
            if v is not None:
                defaults[k] = v
        return defaults

    async def _execute_single_tool(
        self,
        tc: ToolCallInfo,
        seen_calls: dict[str, ToolResultEnvelope],
    ) -> ToolResultEnvelope:
        """Execute a single tool call and return a canonical result envelope.

        Handles predictive cache hits, deduplication, allowlist enforcement,
        confirmation gates, capability token checks, and error normalization.
        """
        # Check predictive cache first — if we already prefetched this
        # exact call speculatively, return the cached result instantly.
        if self._predictive_enabled and not tc.tool_use_id.startswith("predict-"):
            cached = await self._predictive_cache.get(tc.name, tc.input)
            if cached is not None:
                logger.info(
                    "Predictive cache hit: %s (saved %dms)",
                    tc.name,
                    cached.latency_ms,
                )
                return ToolResultEnvelope(
                    call_id=tc.tool_use_id,
                    tool=cached.tool,
                    status=cached.status,
                    result=cached.result,
                    error=cached.error,
                    latency_ms=0,
                    tool_use_id=tc.tool_use_id,
                    raw=cached.raw,
                )

        dedup_key = f"{tc.name}|{json.dumps(tc.input, sort_keys=True)}"
        if dedup_key in seen_calls:
            prev = seen_calls[dedup_key]
            logger.debug("Dedup: skipped duplicate tool call %s", tc.name)
            return ToolResultEnvelope(
                call_id=tc.tool_use_id,
                tool=prev.tool,
                status=prev.status,
                result=prev.result,
                error=prev.error,
                latency_ms=0,
                tool_use_id=tc.tool_use_id,
                raw=prev.raw,
            )

        output_level = self._tool_output_overrides.get(tc.name, self._tool_output_level)
        call = ToolCallEnvelope(
            call_id=tc.tool_use_id,
            agent_id="agent_loop",
            tool=tc.name,
            args=tc.input,
            context=ToolCallContext(
                trace_id=uuid.uuid4().hex,
                policy="default",
                output_level=output_level,
            ),
        )
        started = time.monotonic()

        # Tool allowlist enforcement
        if self._tool_allowlist is not None and tc.name not in self._tool_allowlist:
            allowed = ", ".join(sorted(self._tool_allowlist)[:20])
            err = ToolExecutionError(
                type=ToolErrorType.UNAUTHORIZED,
                message=(
                    f"Tool '{tc.name}' not in allowlist. Available tools: {allowed}"
                ),
                safe_to_retry=False,
            )
            return ToolResultEnvelope(
                call_id=call.call_id,
                tool=call.tool,
                status="error",
                error=err,
                latency_ms=int((time.monotonic() - started) * 1000),
                tool_use_id=tc.tool_use_id,
                raw=tc.raw,
            )

        # Confirmation gate
        if self._on_confirm is not None:
            approved = self._on_confirm(tc)
            if asyncio.iscoroutine(approved) or asyncio.isfuture(approved):
                approved = await approved
            if not approved:
                err = ToolExecutionError(
                    type=ToolErrorType.UNAUTHORIZED,
                    message="Tool call denied by user.",
                    safe_to_retry=False,
                )
                return ToolResultEnvelope(
                    call_id=call.call_id,
                    tool=call.tool,
                    status="error",
                    error=err,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    tool_use_id=tc.tool_use_id,
                    raw=tc.raw,
                )

        spec = self._tools.get(tc.name)
        if spec is None:
            # Track repeated NOT_FOUND to break infinite retry loops
            self._not_found_counts[tc.name] = self._not_found_counts.get(tc.name, 0) + 1
            count = self._not_found_counts[tc.name]

            available = self._tools.names()
            matches = difflib.get_close_matches(tc.name, available, n=3, cutoff=0.4)

            if count >= 3:
                # After 3 failures, give a hard stop message
                msg = (
                    f"STOP: `{tc.name}` does not exist and has failed {count} times. "
                    f"Do NOT call it again. "
                    f"Available tools: {', '.join(available[:20])}"
                )
            elif matches:
                suggestions = ", ".join(f"`{m}`" for m in matches)
                msg = f"Unknown tool: {tc.name}. Did you mean: {suggestions}?"
            else:
                msg = (
                    f"Unknown tool: {tc.name}. Use one of: {', '.join(available[:15])}"
                )
            err = ToolExecutionError(
                type=ToolErrorType.NOT_FOUND,
                message=msg,
                # Retryable if we have suggestions -- let the model self-correct.
                # After 3+ failures, stop retrying (the hard-stop message above).
                safe_to_retry=bool(matches) and count < 3,
            )
            return ToolResultEnvelope(
                call_id=call.call_id,
                tool=call.tool,
                status="error",
                error=err,
                latency_ms=int((time.monotonic() - started) * 1000),
                tool_use_id=tc.tool_use_id,
                raw=tc.raw,
            )

        # Capability token enforcement (defense in depth)
        if self._capability_token is not None:
            try:
                from obscura.auth.capability import validate_capability_token

                if not validate_capability_token(self._capability_token):
                    _audit_tool_denied(tc.name, "invalid_or_expired_token")
                    err = ToolExecutionError(
                        type=ToolErrorType.UNAUTHORIZED,
                        message="Capability token invalid or expired.",
                        safe_to_retry=False,
                    )
                    return ToolResultEnvelope(
                        call_id=call.call_id,
                        tool=call.tool,
                        status="error",
                        error=err,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        tool_use_id=tc.tool_use_id,
                        raw=tc.raw,
                    )

                # Tier-based enforcement removed -- access control is handled
                # by ToolPolicy + CapabilityResolver + ToolBroker.
                pass
            except Exception:
                logger.debug("Capability module unavailable", exc_info=True)

        try:
            from obscura.core.tool_context import (
                ToolContext,
                bind_tool_context,
            )

            # Pull current host callbacks from the legacy module-level globals
            # so tools migrated to ToolContext keep working without any change
            # to the REPL's wiring code.
            ctx = ToolContext(
                registry=self._tools,
                history=self._current_messages,
                user=self._current_user,
                session_id=self._current_session_id,
                mcp_discovery_report=getattr(
                    self._backend, "last_mcp_discovery_report", None
                ),
                **self._read_host_callbacks(),
            )
            with bind_tool_context(ctx):
                result = await self._call_handler(spec, tc.input)

            # Apply output bridge transform if registered
            bridge = TOOL_BRIDGES.get(spec.name)
            if bridge is not None:
                _, output_transform = bridge
                if output_transform is not None and isinstance(result, str):
                    result = output_transform(result)

            envelope = ToolResultEnvelope(
                call_id=call.call_id,
                tool=call.tool,
                status="ok",
                result=result,
                latency_ms=int((time.monotonic() - started) * 1000),
                tool_use_id=tc.tool_use_id,
                raw=tc.raw,
            )
            seen_calls[dedup_key] = envelope
            # Track successful tool calls so the post-turn quality scan can
            # tell whether the model's narration contradicted reality.
            try:
                from obscura.core.output_quality import ToolResultSummary

                snippet = (
                    result
                    if isinstance(result, str)
                    else json.dumps(result, default=str)
                )
                self._this_turn_successful_tools.append(
                    ToolResultSummary(
                        tool_name=call.tool,
                        snippet=snippet[:200],
                    )
                )
            except Exception:
                logger.debug(
                    "suppressed exception in _execute_single_tool", exc_info=True
                )
            return envelope
        except Exception as exc:
            logger.warning("Tool %s failed: %s", tc.name, exc)
            err = self._normalize_tool_error(exc)
            # Enrich INVALID_ARGS errors with the tool's required params
            if err.type == ToolErrorType.INVALID_ARGS:
                required = spec.parameters.get("required", [])
                props = spec.parameters.get("properties", {})
                if required:
                    param_hints = ", ".join(
                        f"`{p}` ({props.get(p, {}).get('type', '?')})" for p in required
                    )
                    err = ToolExecutionError(
                        type=err.type,
                        message=f"{err.message}. Required params: {param_hints}",
                        safe_to_retry=True,
                    )
            envelope = ToolResultEnvelope(
                call_id=call.call_id,
                tool=call.tool,
                status="error",
                error=err,
                latency_ms=int((time.monotonic() - started) * 1000),
                tool_use_id=tc.tool_use_id,
                raw=tc.raw,
            )
            seen_calls[dedup_key] = envelope
            return envelope

    async def _execute_tools(
        self,
        tool_calls: list[ToolCallInfo],
        turn: int | None = None,
        **kwargs: object,
    ) -> list[ToolResultEnvelope]:
        """Execute tool calls and return canonical result envelopes.

        Tool calls are partitioned into batches based on their
        ``ToolSpec.is_concurrency_safe()`` flag.  Consecutive
        side-effect-free tools run concurrently (up to
        :data:`MAX_TOOL_CONCURRENCY`); tools with side effects run
        serially in order.  Result ordering always matches the input
        ``tool_calls`` list.
        """
        # Deduplicate: skip tool calls with identical name+input in the same
        # turn.  LLMs sometimes emit the same call twice.  Reuse the first
        # result so the model sees a consistent response for both call IDs.
        seen_calls: dict[str, ToolResultEnvelope] = {}  # "name|input_json" -> result

        batches = self._partition_tool_calls(tool_calls)

        results: list[ToolResultEnvelope] = []
        for is_concurrent, batch in batches:
            if is_concurrent and len(batch) > 1:
                batch_results = await self._gather_with_limit(
                    [self._execute_single_tool(tc, seen_calls) for tc in batch],
                    MAX_TOOL_CONCURRENCY,
                )
                results.extend(batch_results)
            else:
                for tc in batch:
                    results.append(await self._execute_single_tool(tc, seen_calls))

        return results

    @staticmethod
    async def _call_handler(spec: ToolSpec, inputs: dict[str, Any]) -> Any:
        """Call a tool handler (sync or async).

        Automatically filters out kwargs the handler doesn't declare -- e.g. a
        ``prompt`` param that Claude passes to ``web_fetch`` but other backends
        don't.  This gives cross-agent parity without requiring every tool to
        enumerate every LLM-convention parameter.
        """
        handler = spec.handler

        # Apply structural bridge transforms (cross-backend schema compat)
        bridge = TOOL_BRIDGES.get(spec.name)
        if bridge is not None:
            input_transform, _ = bridge
            if input_transform is not None:
                inputs = input_transform(inputs)

        # Normalize parameter names based on known aliases
        if spec.name in PARAMETER_ALIASES:
            aliases = PARAMETER_ALIASES[spec.name]
            for alias, canonical in aliases.items():
                if alias in inputs:
                    if canonical not in inputs:
                        inputs[canonical] = inputs.pop(alias)
                    else:
                        # Both alias and canonical provided — keep canonical,
                        # warn so we can diagnose if LLM sends conflicting values.
                        logger.warning(
                            "Tool %s: both '%s' (alias) and '%s' (canonical) "
                            "provided; dropping alias value",
                            spec.name,
                            alias,
                            canonical,
                        )
                        del inputs[alias]

        # Pre-validate required params before calling handler — gives a
        # cleaner error than the Python TypeError and avoids a traceback.
        required = spec.parameters.get("required", [])
        if required:
            missing = [p for p in required if p not in inputs]
            if missing:
                props = spec.parameters.get("properties", {})
                hints = ", ".join(
                    f"`{p}` ({props.get(p, {}).get('type', '?')})" for p in missing
                )
                raise TypeError(
                    f"{spec.name}() missing {len(missing)} required "
                    f"positional arguments: {hints}"
                )

        # Coerce inputs to match JSON Schema types.  LLMs frequently send
        # string values for integer/number/boolean params (e.g. "5" instead
        # of 5).  Doing this centrally avoids per-tool int() casts.
        props = spec.parameters.get("properties", {})
        for key, value in list(inputs.items()):
            if value is None:
                continue
            prop_schema = props.get(key)
            if not prop_schema:
                continue
            expected = prop_schema.get("type")
            try:
                if expected == "integer" and not isinstance(value, int):
                    inputs[key] = int(value)
                elif expected == "number" and not isinstance(value, (int, float)):
                    inputs[key] = float(value)
                elif expected == "boolean" and not isinstance(value, bool):
                    if isinstance(value, str):
                        inputs[key] = value.lower() not in ("", "0", "false", "no")
                    else:
                        inputs[key] = bool(value)
                elif expected == "string" and not isinstance(value, str):
                    inputs[key] = str(value)
            except (ValueError, TypeError):
                logger.debug(
                    "Tool %s: could not coerce %s=%r to %s",
                    spec.name,
                    key,
                    value,
                    expected,
                )

        try:
            if inspect.iscoroutinefunction(handler):
                return await handler(**inputs)
            result = handler(**inputs)
            # Handle sync wrappers that return coroutines (e.g. @tool() decorator)
            if asyncio.iscoroutine(result):
                return await result
            return result
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            # If the handler uses **kwargs it accepts everything -- a TypeError
            # here is a genuine bug, not a cross-agent compat issue.
            sig = inspect.signature(handler)
            if any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                raise
            accepted = {
                n
                for n, p in sig.parameters.items()
                if p.kind
                not in (
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                )
            }
            filtered = {k: v for k, v in inputs.items() if k in accepted}
            dropped = sorted(set(inputs) - accepted)
            logger.debug("Tool %s: dropping undeclared kwargs %s", spec.name, dropped)
            if inspect.iscoroutinefunction(handler):
                return await handler(**filtered)
            result = handler(**filtered)
            if asyncio.iscoroutine(result):
                return await result
            return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_tool_error(exc: Exception) -> ToolExecutionError:
        msg = str(exc)
        lower = msg.lower()

        if isinstance(exc, (TypeError, ValueError)):
            if (
                "required positional argument" in lower
                or "missing" in lower
                or "unexpected keyword argument" in lower
            ):
                return ToolExecutionError(
                    type=ToolErrorType.INVALID_ARGS,
                    message=msg,
                    safe_to_retry=True,
                )
        if isinstance(exc, PermissionError):
            return ToolExecutionError(
                type=ToolErrorType.UNAUTHORIZED,
                message=msg,
                safe_to_retry=False,
            )
        if isinstance(exc, FileNotFoundError):
            return ToolExecutionError(
                type=ToolErrorType.NOT_FOUND,
                message=msg,
                safe_to_retry=False,
            )
        if isinstance(exc, FileExistsError):
            return ToolExecutionError(
                type=ToolErrorType.CONFLICT,
                message=msg,
                safe_to_retry=False,
            )
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return ToolExecutionError(
                type=ToolErrorType.TIMEOUT,
                message=msg,
                safe_to_retry=True,
            )
        if isinstance(exc, ConnectionError):
            return ToolExecutionError(
                type=ToolErrorType.DEPENDENCY_ERROR,
                message=msg,
                safe_to_retry=True,
            )
        if "rate limit" in lower or "429" in lower:
            return ToolExecutionError(
                type=ToolErrorType.RATE_LIMITED,
                message=msg,
                safe_to_retry=True,
            )
        return ToolExecutionError(
            type=ToolErrorType.UNKNOWN,
            message=msg,
            safe_to_retry=False,
        )

    @staticmethod
    def _render_tool_result_text(result: ToolResultEnvelope) -> str:
        """Render a ToolResultEnvelope as text for the model.

        Large results are written to disk and replaced with a truncated
        preview (see :func:`_maybe_truncate_result`).
        """

        def _encode(obj: Any) -> str:
            """Encode as TOON (~40% fewer tokens), fall back to JSON."""
            try:
                import toons

                return toons.dumps(obj)
            except Exception:
                logger.debug("suppressed exception in _encode", exc_info=True)
                return json.dumps(obj, default=str)

        if result.status == "ok":
            text = (
                result.result
                if isinstance(result.result, str)
                else _encode(result.result)
            )
            return _maybe_truncate_result(text, result.tool, result.tool_use_id)
        if result.error is None:
            return "Tool error"
        payload = {
            "type": result.error.type.value,
            "message": result.error.message,
            "retry_after_ms": result.error.retry_after_ms,
            "safe_to_retry": result.error.safe_to_retry,
        }
        return _maybe_truncate_result(_encode(payload), result.tool, result.tool_use_id)

    @staticmethod
    def cleanup_result_cache(max_age_hours: int = 24) -> int:
        """Remove cached tool results older than *max_age_hours*.

        Returns the number of files deleted.
        """
        if not TOOL_RESULT_CACHE_DIR.exists():
            return 0

        cutoff = time.time() - max_age_hours * 3600
        deleted = 0
        for path in TOOL_RESULT_CACHE_DIR.iterdir():
            try:
                if path.is_file() and os.path.getmtime(path) < cutoff:
                    path.unlink()
                    deleted += 1
            except OSError:
                logger.debug("Failed to remove cached result %s", path, exc_info=True)
        return deleted

    @staticmethod
    def _map_chunk(chunk: StreamChunk, turn: int) -> AgentEvent | None:
        """Map a StreamChunk to an AgentEvent, or None to skip."""
        if chunk.kind == ChunkKind.TEXT_DELTA:
            return AgentEvent(
                kind=AgentEventKind.TEXT_DELTA,
                text=chunk.text,
                turn=turn,
                raw=chunk.raw,
            )
        if chunk.kind == ChunkKind.THINKING_DELTA:
            return AgentEvent(
                kind=AgentEventKind.THINKING_DELTA,
                text=chunk.text,
                turn=turn,
                raw=chunk.raw,
            )
        # Note: TOOL_CALL events are emitted *after* the full tool call
        # is parsed (in the main loop), not here at TOOL_USE_START.
        # This ensures the persisted TOOL_CALL includes tool_input.
        if chunk.kind == ChunkKind.ERROR:
            return AgentEvent(
                kind=AgentEventKind.ERROR,
                text=chunk.text,
                turn=turn,
                raw=chunk.raw,
                metadata=chunk.metadata,
            )
        return None

    @staticmethod
    def _parse_tool_call(
        name: str,
        input_json: str,
        raw: Any,
    ) -> ToolCallInfo:
        """Parse accumulated tool call data into a ToolCallInfo."""
        raw_input = AgentLoop._extract_tool_input_from_raw(raw)
        parsed_input: dict[str, Any] = {}
        delta_valid = False

        if input_json.strip():
            try:
                decoded = json.loads(input_json)
                if isinstance(decoded, dict):
                    parsed_input = cast(dict[str, Any], decoded)
                    delta_valid = True
                else:
                    logger.warning(
                        "Tool %s: JSON input decoded to %s, not dict",
                        name,
                        type(decoded).__name__,
                    )
                    parsed_input = {"_raw_input": input_json}
            except json.JSONDecodeError as jde:
                logger.warning(
                    "Tool %s: malformed JSON input (%.80s…): %s",
                    name,
                    input_json,
                    jde,
                )
                parsed_input = {"_raw_input": input_json}

        if delta_valid:
            # Streamed JSON deltas are authoritative when present.
            parsed_input = {**raw_input, **parsed_input}
        elif not parsed_input:
            parsed_input = raw_input
        elif raw_input and parsed_input.keys() == {"_raw_input"}:
            # If delta payload is invalid, prefer provider-native structured args.
            parsed_input = raw_input

        # Normalize dotted provider prefixes (e.g. "functions.web_search") to
        # the canonical tool name expected by the runtime ("web_search").
        # Skip mcp__-prefixed names: those carry meaningful structure that
        # ToolRegistry.get() resolves on its own (dot↔underscore variants),
        # and stripping here would discard the server-name segment.
        if "." in name and not name.startswith("mcp__"):
            name = name.rsplit(".", maxsplit=1)[-1]

        return ToolCallInfo(
            tool_use_id=f"tool_{uuid.uuid4().hex[:12]}",
            name=name,
            input=parsed_input,
            raw=raw,
        )

    @staticmethod
    def _extract_tool_input_from_raw(raw: Any) -> dict[str, Any]:
        """Best-effort extraction of tool args from provider-native raw events."""

        def _field(obj: Any, key: str) -> Any:
            if obj is None:
                return None
            if isinstance(obj, dict):
                return cast(dict[str, Any], obj).get(key)
            return getattr(obj, key, None)

        def _coerce_to_dict(value: Any) -> dict[str, Any]:
            if value is None:
                return {}
            if isinstance(value, dict):
                return cast(dict[str, Any], value)
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    return {}
                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError:
                    logger.debug(
                        "suppressed exception in _coerce_to_dict", exc_info=True
                    )
                    return {"_raw_input": value}
                if isinstance(decoded, dict):
                    return cast(dict[str, Any], decoded)
                return {"_raw_input": value}
            model_dump = getattr(value, "model_dump", None)
            if callable(model_dump):
                dumped = model_dump()
                if isinstance(dumped, dict):
                    return cast(dict[str, Any], dumped)
                return {}
            to_dict = getattr(value, "to_dict", None)
            if callable(to_dict):
                dumped = to_dict()
                if isinstance(dumped, dict):
                    return cast(dict[str, Any], dumped)
                return {}
            as_dict = getattr(value, "__dict__", None)
            if isinstance(as_dict, dict):
                object_dict = cast(dict[str, Any], as_dict)
                return {k: v for k, v in object_dict.items() if not k.startswith("_")}
            return {}

        data = _field(raw, "data")
        if data is None:
            data = raw

        direct_keys = ("tool_input", "input", "arguments", "parameters")
        for key in direct_keys:
            payload = _field(data, key)
            parsed = _coerce_to_dict(payload)
            if parsed:
                return parsed

        nested_tool = _field(data, "tool_call")
        if nested_tool is not None:
            for key in direct_keys:
                payload = _field(nested_tool, key)
                parsed = _coerce_to_dict(payload)
                if parsed:
                    return parsed

        return {}

    @staticmethod
    def _build_structured_tool_messages(
        tool_calls: list[ToolCallInfo],
        tool_results: list[ToolResultEnvelope],
        turn_text: str,
    ) -> list[Message]:
        """Build structured Message objects for tool call/result turns.

        Returns two messages:
        1. Assistant message with text (if any) + tool_use blocks
        2. Tool result message with tool_result blocks

        These are passed to ``backend.stream(prompt, messages=...)``
        so backends can persist the full structured conversation.
        """
        # 1) Assistant message: any text + tool_use blocks
        assistant_blocks: list[Any] = []
        if turn_text:
            assistant_blocks.append(TextBlock(text=turn_text))
        for tc in tool_calls:
            assistant_blocks.append(
                ToolUseBlock(
                    tool_name=tc.name,
                    args=tc.input,
                    tool_use_id=tc.tool_use_id,
                )
            )
        if not assistant_blocks:
            assistant_blocks.append(TextBlock(text=""))

        assistant_msg = Message(
            role=Role.ASSISTANT,
            content=assistant_blocks,
        )

        # 2) Tool result message
        result_blocks: list[Any] = []
        result_by_id = {r.tool_use_id: r for r in tool_results}
        for tc in tool_calls:
            result = result_by_id.get(tc.tool_use_id)
            if result is None:
                # Emit an explicit error so the LLM knows the call had no result
                logger.warning(
                    "Tool result missing for %s (id=%s)",
                    tc.name,
                    tc.tool_use_id,
                )
                result_blocks.append(
                    ToolResultBlock(
                        content=f"Internal error: no result received for {tc.name}. "
                        "The tool call may have been dropped or timed out.",
                        tool_use_id=tc.tool_use_id,
                        is_error=True,
                    )
                )
                continue
            result_blocks.append(
                ToolResultBlock(
                    content=AgentLoop._render_tool_result_text(result),
                    tool_use_id=tc.tool_use_id,
                    is_error=result.status == "error",
                )
            )

        result_msg = Message(
            role=Role.TOOL_RESULT,
            content=result_blocks,
        )

        return [assistant_msg, result_msg]

    @staticmethod
    def _is_context_too_long(exc: Exception) -> bool:
        """Check if an exception indicates the prompt/context is too large."""
        msg = str(exc).lower()
        return any(
            pattern in msg
            for pattern in (
                "prompt_too_long",
                "prompt too long",
                "prompt is too long",
                "context_length_exceeded",
                "too many tokens",
                "maximum context length",
                "request too large",
                "input too long",
            )
        )

    async def _reactive_compact(
        self,
        kwargs: dict[str, Any],
        state: TurnState,
    ) -> tuple[dict[str, Any], TurnState] | None:
        """Attempt to compact message history mid-turn to recover from prompt_too_long.

        Returns updated kwargs and state, or None if compaction isn't possible.
        """
        messages: list[Message] = kwargs.get("messages", [])
        if len(messages) <= 2:
            return None  # Nothing to compact

        # Keep the last 2 message pairs (4 messages), summarize the rest
        keep_count = min(4, len(messages))
        keep = messages[-keep_count:]
        old = messages[:-keep_count]

        if not old:
            return None

        # Build summary of compacted messages
        tool_names: set[str] = set()
        for msg in old:
            for block in msg.content:
                tn = getattr(block, "tool_name", None)
                if tn:
                    tool_names.add(tn)

        summary_text = (
            f"[Context compacted mid-turn: {len(old)} earlier messages removed. "
            f"Tools used: {', '.join(sorted(tool_names)) if tool_names else 'none'}. "
            f"Focus on the most recent context below.]"
        )

        summary_msg = Message(
            role=Role.USER,
            content=[TextBlock(text=summary_text)],
        )

        new_messages = [summary_msg, *keep]
        new_kwargs = {**kwargs, "messages": new_messages}

        # Reset accumulated chars/tokens and mark compaction as attempted
        new_state = state.replace(
            accumulated_text="",
            input_tokens=0,
            output_tokens=0,
            has_attempted_reactive_compact=True,
        )

        logger.warning(
            "Reactive compaction on turn %d: removed %d messages, kept %d",
            state.turn,
            len(old),
            keep_count,
        )

        return new_kwargs, new_state

    def _compact_messages(
        self,
        kwargs: dict[str, Any],
    ) -> tuple[dict[str, Any], int, int]:
        """Compact accumulated structured messages to stay within budget.

        Keeps the last 2 message pairs (4 Messages) and replaces older
        pairs with a brief text summary.

        Returns (updated_kwargs, dropped_pairs, freed_chars).
        """
        messages: list[Message] = kwargs.get("messages", [])
        if len(messages) <= 4:
            return kwargs, 0, 0

        # Messages come in pairs: (assistant, tool_result)
        keep = messages[-4:]  # last 2 pairs
        old = messages[:-4]

        # Estimate chars being freed
        old_chars = 0
        tool_names: list[str] = []
        for msg in old:
            for block in msg.content:
                old_chars += len(getattr(block, "text", "") or "")
                if getattr(block, "tool_name", None):
                    tool_names.append(block.tool_name)

        # Unique tool names for summary
        seen: set[str] = set()
        unique_tools: list[str] = []
        for t in tool_names:
            if t not in seen:
                seen.add(t)
                unique_tools.append(t)

        dropped_pairs = len(old) // 2
        summary = (
            f"[Compacted: {dropped_pairs} earlier tool turns. "
            f"Tools used: {', '.join(unique_tools[:10])}]"
        )

        summary_msg = Message(
            role=Role.ASSISTANT,
            content=[TextBlock(text=summary)],
        )

        new_kwargs = {**kwargs, "messages": [summary_msg, *keep]}
        self._accumulated_chars = sum(
            len(getattr(b, "text", "") or "") for m in keep for b in m.content
        )
        return new_kwargs, dropped_pairs, old_chars

    # Fix #5: _format_tool_results and _format_tool_results_envelopes are
    # kept for backward compatibility (public test wrappers, reconstruct_state)
    # but are no longer used in the main loop.  The loop now uses only
    # structured messages with an empty continuation prompt.

    @staticmethod
    def _format_tool_results(
        results: list[tuple[ToolCallInfo, str, bool]],
    ) -> str:
        """Format tool results as a prompt for the next model turn.

        .. deprecated::
            Retained for backward compatibility with tests and
            ``reconstruct_state``.  The main loop now uses only
            structured messages (Fix #5).
        """
        envelopes: list[ToolResultEnvelope] = []
        for tc, result_text, is_error in results:
            envelopes.append(
                ToolResultEnvelope(
                    call_id=tc.tool_use_id,
                    tool=tc.name,
                    status="error" if is_error else "ok",
                    result=None if is_error else result_text,
                    error=(
                        ToolExecutionError(
                            type=ToolErrorType.UNKNOWN,
                            message=result_text,
                            safe_to_retry=False,
                        )
                        if is_error
                        else None
                    ),
                    tool_use_id=tc.tool_use_id,
                    raw=tc.raw,
                )
            )
        return AgentLoop._format_tool_results_envelopes(envelopes)

    @staticmethod
    def _format_tool_results_envelopes(results: list[ToolResultEnvelope]) -> str:
        """Format canonical tool result envelopes as prompt text.

        .. deprecated::
            Retained for backward compatibility with tests and
            ``reconstruct_state``.  The main loop now uses only
            structured messages (Fix #5).
        """
        parts: list[str] = [
            "<system>The following are results from the tool calls you just made. "
            "Do NOT repeat or echo these results back to the user.</system>",
        ]
        for result in results:
            status = "error" if result.status == "error" else "success"
            result_text = AgentLoop._render_tool_result_text(result)
            # Include an explicit status marker line for LLM-facing formatting
            # so tests and downstream parsers can look for "OK" / "ERROR".
            status_label = "ERROR" if status == "error" else "OK"
            parts.append(
                f'<tool_result tool="{result.tool}" '
                f'id="{result.tool_use_id}" status="{status}">\n'
                f"{status_label}\n"
                f"{result_text}\n"
                f"</tool_result>"
            )
        return "\n\n".join(parts)

    # Public test/observability wrappers ---------------------------------
    @staticmethod
    def parse_tool_call(name: str, input_json: str, raw: Any) -> ToolCallInfo:
        """Public wrapper to parse a tool call (testing)."""
        return AgentLoop._parse_tool_call(name, input_json, raw)

    @staticmethod
    def format_tool_results(results: list[tuple[ToolCallInfo, str, bool]]) -> str:
        """Public wrapper to format tool results (testing)."""
        return AgentLoop._format_tool_results(results)

    @staticmethod
    def reconstruct_state(
        events: list[EventRecord],
    ) -> tuple[int, str, list[Message], str]:
        """Reconstruct loop state from persisted events for resume.

        Returns
        -------
        (turn, accumulated_text, messages, last_prompt)
            - turn: last completed turn number
            - accumulated_text: all text deltas concatenated
            - messages: structured tool call/result Message pairs
            - last_prompt: the most recent prompt text (empty string for
              structured-only continuation)
        """
        turn = 0
        accumulated_text = ""
        last_prompt = ""
        messages: list[Message] = []

        # Group tool calls and results per turn for structured message rebuild
        current_turn_tool_calls: list[ToolCallInfo] = []
        current_turn_tool_results: list[ToolResultEnvelope] = []
        current_turn_text = ""
        current_turn = 0

        for rec in events:
            kind_str = rec.payload.get("kind", "")
            event_turn = int(rec.payload.get("turn", 0))

            # When we enter a new turn, flush any accumulated tool pairs
            if event_turn > current_turn > 0:
                if current_turn_tool_calls and current_turn_tool_results:
                    pair = AgentLoop._build_structured_tool_messages(
                        current_turn_tool_calls,
                        current_turn_tool_results,
                        current_turn_text,
                    )
                    messages.extend(pair)
                    # Fix #5: use empty prompt for structured continuation
                    last_prompt = ""
                current_turn_tool_calls = []
                current_turn_tool_results = []
                current_turn_text = ""
                current_turn = event_turn

            if kind_str == AgentEventKind.TURN_COMPLETE.value:
                turn = event_turn

            elif kind_str == AgentEventKind.TEXT_DELTA.value:
                text = str(rec.payload.get("text", ""))
                accumulated_text += text
                current_turn_text += text

            elif kind_str == AgentEventKind.TOOL_CALL.value:
                raw_input = rec.payload.get("tool_input")
                tool_input: dict[str, Any] = (
                    cast(dict[str, Any], raw_input)
                    if isinstance(raw_input, dict)
                    else {}
                )
                tc = ToolCallInfo(
                    tool_use_id=str(rec.payload.get("tool_use_id", "")),
                    name=str(rec.payload.get("tool_name", "")),
                    input=tool_input,
                )
                current_turn_tool_calls.append(tc)

            elif kind_str == AgentEventKind.TOOL_RESULT.value:
                result_text = str(rec.payload.get("tool_result", ""))
                is_error = bool(rec.payload.get("is_error", False))
                tr = ToolResultEnvelope(
                    call_id=str(rec.payload.get("tool_use_id", "")),
                    tool=str(rec.payload.get("tool_name", "")),
                    status="error" if is_error else "ok",
                    result=None if is_error else result_text,
                    error=(
                        ToolExecutionError(
                            type=ToolErrorType.UNKNOWN,
                            message=result_text,
                            safe_to_retry=False,
                        )
                        if is_error
                        else None
                    ),
                    tool_use_id=str(rec.payload.get("tool_use_id", "")),
                )
                current_turn_tool_results.append(tr)

            elif kind_str == AgentEventKind.USER_INPUT.value:
                last_prompt = str(rec.payload.get("text", ""))

            # Track current turn
            if event_turn > 0:
                current_turn = event_turn

        # Flush final turn's tool pairs
        if current_turn_tool_calls and current_turn_tool_results:
            pair = AgentLoop._build_structured_tool_messages(
                current_turn_tool_calls,
                current_turn_tool_results,
                current_turn_text,
            )
            messages.extend(pair)
            # Fix #5: use empty prompt for structured continuation
            last_prompt = ""

        return turn, accumulated_text, messages, last_prompt


# ---------------------------------------------------------------------------
# Audit helper for capability enforcement
# ---------------------------------------------------------------------------


def _audit_tool_denied(tool_name: str, reason: str) -> None:
    """Emit an audit event when a tool call is denied by capability enforcement."""
    try:
        from obscura.telemetry.audit import AuditEvent, emit_audit_event

        emit_audit_event(
            AuditEvent(
                event_type="tool.denied",
                user_id="agent_loop",
                user_email="",
                resource=f"tool:{tool_name}",
                action="execute",
                outcome="denied",
                details={"reason": reason},
            )
        )
    except Exception:
        logger.debug("Failed to emit audit event for denied tool", exc_info=True)


async def call_tool_handler(spec: ToolSpec, inputs: dict[str, Any]) -> Any:
    """Dispatch a tool call through the shared bridging pipeline.

    Thin module-level entry point so every dispatch site — the agent loop,
    the claude provider wrapper, the copilot provider wrapper — shares one
    definition of bridging, aliasing, validation, coercion, and
    undeclared-kwarg tolerance. Implementation lives on
    :meth:`AgentLoop._call_handler`.
    """
    return await AgentLoop._call_handler(spec, inputs)  # pyright: ignore[reportPrivateUsage]
