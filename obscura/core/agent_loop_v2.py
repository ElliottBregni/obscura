"""obscura.core.agent_loop_v2 — DAG-native agent loop (clean rewrite).

This is the successor to :class:`obscura.core.agent_loop.AgentLoop` (v1).
v1 is ~10K lines and grew organically: a streaming tool executor, predictive
cache, capability gates, retries, hooks, arbiter integration, and the
core turn loop are all interleaved. v2 separates the core loop from the
optional behaviors and uses :mod:`obscura.core.dag` natively for tool
execution.

Architectural diff from v1
==========================

+--------------------------+----------------------------------+---------------------------------+
| Concern                  | v1                               | v2                              |
+==========================+==================================+=================================+
| Tool dispatch            | StreamingToolExecutor            | dag.Scheduler                   |
| Intra-turn parallelism   | side_effects=="none" only        | DAG edges + concurrency caps    |
| Retries / backoff        | inline in run()                  | RetryingBackend wrapper         |
| Predictive cache         | inline in run()                  | predictive_cache_middleware     |
| Capability gates         | inline _execute_single_tool      | capability_gate middleware      |
| Arbiter (turn-level)     | inline                           | arbiter_post_turn hook          |
| Tool confirmation        | inline + on_confirm callback     | tool_confirmation middleware    |
| Tool allow/deny          | inline                           | tool_allowlist / tool_denylist  |
| Hooks (pre/post tool)    | inline HookRegistry calls        | hook_middleware                 |
| Compaction               | inline                           | compact_pre_turn hook           |
| Event store              | inline                           | event_store_post_turn hook      |
| host_callbacks           | ToolContext fields               | ToolContext fields (parity)     |
| compiled_agent           | inline                           | factory translation             |
| seen_calls dedup         | StreamingToolExecutor.seen_calls | _seen_calls dict (load-bearing) |
| Cancellation             | abort_event + task.cancel        | scheduler.cancel_event          |
+--------------------------+----------------------------------+---------------------------------+

What v2 owns
------------

A focused, ~600-line implementation that:

1. Streams from a :class:`BackendProtocol`, one turn at a time.
2. Collects ``tool_use`` blocks during the stream into ``ToolCallInfo`` objects.
3. Fires text-delta observers (predictive cache observer is the canonical user).
4. After the assistant turn ends, builds a :class:`TurnDAG` from the collected
   calls (with no edges by default — matches today's batch behavior).
5. Runs the DAG through :class:`Scheduler`, sequential or parallel depending
   on whether any node has declared ``depends_on``.
6. Binds a :class:`ToolContext` per turn so tools see host_callbacks /
   history / session_id / registry just like v1.
7. Composes any user-supplied dispatch_middleware around the per-node
   executor (capability_gate, tool_confirmation, predictive_cache_middleware,
   etc.).
8. Yields :class:`AgentEvent` instances throughout (TEXT_DELTA, TOOL_CALL,
   TOOL_RESULT, AGENT_DONE) — same shape as v1 so callers don't need
   to change.
9. Repeats until the model emits no tool calls or ``max_turns`` is exceeded.

Migration
---------

**v2 is the default loop**. Existing call sites should migrate to
:func:`obscura.core.agent_loop_factory.make_agent_loop`, which translates
v1-shape kwargs to v2 middleware/hooks automatically. Set
``OBSCURA_AGENT_LOOP=v1`` to revert to the legacy loop while debugging.

v1 (:class:`obscura.core.agent_loop.AgentLoop`) is preserved as a fallback
and deletion target. The deprecation pass and final removal happen once
eval data confirms parity across all real-world workloads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from obscura.core.dag import (
    DAGNode,
    DAGNodeResult,
    Scheduler,
    TurnDAG,
)
from obscura.core.parallel_plan import (
    ParallelPlanValidationError,
    build_turn_dag_from_parallel_plan,
    parse_parallel_plan_input,
)
from obscura.core.tool_context import ToolContext, bind_tool_context
from obscura.core.types import (
    AgentEvent,
    AgentEventKind,
    BackendProtocol,
    ChunkKind,
    ContentBlock,
    Message,
    Role,
    ToolCallInfo,
    ToolSpec,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from obscura.core.tools import ToolRegistry


logger = logging.getLogger(__name__)


# Known ToolContext fields the host_callbacks dict can populate directly.
# Anything else in host_callbacks lands in ToolContext.extras for tool
# code that reads custom keys.
_TOOL_CTX_KNOWN_FIELDS: frozenset[str] = frozenset(
    {
        "ask_user_callback",
        "user_interact_callback",
        "permission_mode_callback",
        "plan_approval_callback",
        "mcp_discovery_report",
    }
)


# Kwargs callers historically passed to ``loop.run()`` that are loop-level
# concerns, NOT backend params. Stripped before forwarding kwargs to
# ``backend.stream()`` so the backend doesn't receive callbacks / event
# stores / etc. that it doesn't know what to do with.
_LOOP_ONLY_KWARGS: frozenset[str] = frozenset(
    {
        "auto_complete",
        "event_store",
        "on_confirm",
        "max_turns",
        "agent_name",
        "context_budget",
        "tool_allowlist",
        "tool_output_level",
        "tool_output_overrides",
        "host_callbacks",
        "compiled_agent",
        "capability_token",
        "hooks",
    }
)


# ---------------------------------------------------------------------------
# Middleware + hook types
# ---------------------------------------------------------------------------


# Per-node executor — the chain that runs for each DAG node. Middleware
# wraps this. The default chain is ``AgentLoopV2._dispatch_node`` adapted
# to the Scheduler's NodeExecutor signature.
NodeExecutorAsync = "Callable[[DAGNode, dict[str, Any]], Awaitable[list[ContentBlock]]]"


# A dispatch-level middleware: a function that takes an inner executor
# and returns a wrapped one. Middleware run outermost-first. They're a
# cheap, composable extension point — capability gates, confirmation
# prompts, predictive cache, hook calls, output filters, etc.
DispatchMiddleware = "Callable[[Callable[..., Any]], Callable[..., Any]]"


@dataclass
class TurnContext:
    """Mutable per-turn state passed to ``pre_turn`` / ``post_turn`` hooks.

    Hooks may mutate ``messages`` (e.g. compaction trims older turns) and
    set ``stop_after_turn`` to True to terminate the loop after the
    current turn finishes (e.g. arbiter kill). Other fields are
    informational.
    """

    turn_index: int
    messages: list[Message]
    cancel_event: asyncio.Event
    stop_after_turn: bool = False


@dataclass(frozen=True)
class TurnResult:
    """Read-only snapshot passed to ``post_turn`` hooks after a turn.

    Captures what the model produced and how the DAG executed, so a
    post-turn hook (arbiter, telemetry, etc.) can react.
    """

    turn_index: int
    text: str
    tool_calls: tuple[ToolCallInfo, ...]
    results: tuple[DAGNodeResult, ...]


# Hook signatures.
# pre_turn: invoked once before each model stream.
# post_turn: invoked once after each turn's tools have all completed.
PreTurnHook = "Callable[[TurnContext], Awaitable[None]]"
PostTurnHook = "Callable[[TurnContext, TurnResult], Awaitable[None]]"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentLoopV2Config:
    """Tuning parameters for :class:`AgentLoopV2`.

    Defaults match what most callers want. ``max_concurrency`` only applies
    when the DAG has declared dependencies (otherwise the scheduler runs
    sequentially in submission order, matching v1).
    """

    max_turns: int = 10
    max_concurrency: int = 8
    per_tool_concurrency: dict[str, int] = field(default_factory=dict)
    per_capability_concurrency: dict[str, int] = field(default_factory=dict)
    parallel_plan_tool_name: str = "parallel_plan"

    # When the model mixes a parallel_plan tool_use with regular tool_use
    # blocks in one turn, the v2 default merges them into a single DAG:
    # the plan's declared dependencies are honored, sibling tool_uses run
    # with NO declared edges (in parallel with plan no-dep nodes).
    #
    # Set this flag to True to inject edges from every sibling to every
    # plan terminal node — guaranteeing siblings observe plan results
    # before they run, at the cost of removing intra-turn parallelism
    # between siblings and plan nodes. Use this for deployments where
    # mixed turns are rare and the safer ordering is worth the latency.
    siblings_wait_for_plan: bool = False


# ---------------------------------------------------------------------------
# AgentLoopV2
# ---------------------------------------------------------------------------


class AgentLoopV2:
    """The DAG-native agent loop.

    Usage::

        loop = AgentLoopV2(backend, registry)
        async for event in loop.run("Fix the bug", session_id="sess-1"):
            ...

    The loop is intentionally small. Capability checks, predictive caching,
    arbiter integration, etc. are added by wrapping with middleware (or by
    using v1 until middleware exists for v2).
    """

    def __init__(
        self,
        backend: BackendProtocol,
        registry: ToolRegistry,
        *,
        config: AgentLoopV2Config | None = None,
        cancel_event: asyncio.Event | None = None,
        dispatch_middleware: list[Callable[[Any], Any]] | None = None,
        pre_turn: Callable[[TurnContext], Awaitable[None]] | None = None,
        post_turn: Callable[[TurnContext, TurnResult], Awaitable[None]] | None = None,
        host_callbacks: dict[str, Any] | None = None,
        text_delta_observers: list[Callable[[str], Awaitable[None]]] | None = None,
        on_turn_start: Callable[[int, ToolContext], Awaitable[None]] | None = None,
        system_prompt: str = "",
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._config = config or AgentLoopV2Config()
        self._cancel_event = cancel_event or asyncio.Event()
        self._dispatch_middleware = list(dispatch_middleware or [])
        self._pre_turn = pre_turn
        self._post_turn = post_turn
        # Per-instance host callbacks threaded into ToolContext on every
        # tool dispatch. v1 parity — tools that read
        # ``current_tool_context().ask_user_callback`` etc. work under v2.
        # Empty dict means no host callbacks; tools fall back to whatever
        # legacy module-level state they support (UI.set_*_callback etc).
        self._host_callbacks: dict[str, Any] = dict(host_callbacks or {})
        # Text-delta observers fire for each TEXT_DELTA chunk. The
        # predictive cache uses this to start speculative dispatches as
        # the model streams text. Observers are awaited inline — keep
        # them lightweight (or use create_task internally for fire-and-forget).
        self._text_delta_observers: list[Callable[[str], Awaitable[None]]] = list(
            text_delta_observers or []
        )
        # Called once per turn at the top, after the per-turn ToolContext
        # is built. Predictive cache uses this to register its observer
        # with the right ToolContext bound (so speculative dispatches
        # see host_callbacks/registry/etc.). Lightweight — runs sync
        # path inline.
        self._on_turn_start = on_turn_start
        # Prepended to the conversation as a SYSTEM-role message before
        # the first user prompt. Empty string = no system message.
        # Mirrors v1's compiled-agent ``instructions`` field.
        self._system_prompt = system_prompt

        # Per-turn dedup keyed by SDK tool_use_id. LOAD-BEARING for
        # correctness — see the extended note on
        # StreamingToolExecutor.seen_calls. If the model's stream is
        # interrupted mid-turn and retried, a tool_use_id seen earlier
        # returns its cached envelope instead of re-executing the
        # side-effecting tool (e.g. ``git commit``).
        #
        # Reset at the top of each fresh turn (NOT across stream retries
        # within the same turn — those keep the cache to dedupe). Empty
        # ``tool_use_id`` (synthesized parallel_plan child nodes) is
        # never deduped: those have no SDK identity, no retry concern.
        self._seen_calls: dict[str, _ToolEnvelopeV2] = {}

    # -- Public ----------------------------------------------------------------

    async def run(
        self,
        prompt: str,
        *,
        session_id: str = "",
        history: list[Message] | None = None,
        initial_messages: list[Message] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AgentEvent]:
        """Drive the agent until the model emits no tool calls or ``max_turns``.

        ``initial_messages`` is a v1-compat alias for ``history``.
        Unrecognized loop-level kwargs (``auto_complete``, ``event_store``,
        ``on_confirm``, ``max_turns``) are silently dropped — those should
        be set at construction time via the factory or AgentLoopV2Config.
        Everything else is forwarded to ``backend.stream()`` so per-call
        backend params (``max_thinking_tokens``, model overrides, etc.)
        flow through to the SDK.
        """
        session_id = session_id or str(uuid.uuid4())
        # initial_messages takes precedence over history when both are passed
        # (matches v1's behavior). Either supplies the prefix conversation.
        history_arg = initial_messages if initial_messages is not None else history
        messages: list[Message] = list(history_arg or [])

        # Strip loop-level kwargs that shouldn't reach the backend.
        # Everything else flows through to backend.stream() as **kwargs.
        backend_kwargs = {
            k: v for k, v in kwargs.items() if k not in _LOOP_ONLY_KWARGS
        }
        # Prepend the system prompt only when no history was provided —
        # if the caller already passed a history, assume it includes any
        # system message they wanted.
        if self._system_prompt and not history:
            messages.insert(
                0,
                Message(
                    role=Role.SYSTEM,
                    content=[ContentBlock(kind="text", text=self._system_prompt)],
                ),
            )
        messages.append(
            Message(role=Role.USER, content=[ContentBlock(kind="text", text=prompt)])
        )

        async for event in self._run_inner(messages, session_id, backend_kwargs):
            yield event

    # -- Internal --------------------------------------------------------------

    async def _run_inner(
        self,
        messages: list[Message],
        session_id: str,
        backend_kwargs: dict[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        bk_kwargs = backend_kwargs or {}
        for turn in range(1, self._config.max_turns + 1):
            if self._cancel_event.is_set():
                yield AgentEvent(
                    kind=AgentEventKind.AGENT_DONE,
                    turn=turn,
                    text="cancelled by caller",
                )
                return

            # Reset stream-retry dedup at the top of each fresh turn.
            # Stream retries within a turn keep the cache (handled by the
            # backend, not the loop — v2 doesn't yet implement intra-turn
            # retry; that's a migration TODO).
            self._seen_calls.clear()

            # Build the per-turn ToolContext early — text-delta observers
            # and the dispatch chain both need it. Reused for every node
            # in this turn (registry/history/session_id are turn-invariant).
            turn_tool_ctx = self._build_tool_context(
                messages=messages, session_id=session_id
            )

            # on_turn_start callback — predictive cache uses this to
            # rebind its observer with the per-turn ToolContext.
            if self._on_turn_start is not None:
                try:
                    await self._on_turn_start(turn, turn_tool_ctx)
                except Exception:
                    logger.exception("on_turn_start raised — swallowing")

            # pre_turn hook — may mutate messages (e.g. compaction trims
            # older turns) or set stop_after_turn to True (e.g. arbiter
            # kill).
            turn_ctx = TurnContext(
                turn_index=turn,
                messages=messages,
                cancel_event=self._cancel_event,
            )
            if self._pre_turn is not None:
                await self._pre_turn(turn_ctx)
                # The hook may have replaced messages in-place; re-bind the
                # local ref. (Keeping ``messages`` as the canonical name so
                # the rest of the function reads the same.)
                messages = turn_ctx.messages

            # Always emit TURN_START at the top of each turn so the
            # renderer can start its "thinking" spinner / clear its
            # frame buffer. Some backends (Claude SDK in particular)
            # don't reliably emit ChunkKind.MESSAGE_START so we can't
            # rely on chunk-level signaling alone.
            yield AgentEvent(
                kind=AgentEventKind.TURN_START,
                turn=turn,
            )

            # Stream the next assistant turn.
            text_buf: list[str] = []
            tool_calls: list[ToolCallInfo] = []
            partial_inputs: dict[
                str, list[str]
            ] = {}  # tool_use_id -> JSON delta chunks
            partial_names: dict[str, str] = {}

            # Extract the latest user-message text as the positional ``prompt``
            # arg for backends whose ``stream()`` signature is
            # ``stream(prompt, **kwargs)``. Pass full ``messages`` as a kwarg
            # so backends that consult history get it. v1 parity.
            latest_user_text = ""
            for msg in reversed(messages):
                if msg.role == Role.USER:
                    for block in msg.content:
                        if block.kind == "text" and block.text:
                            latest_user_text = block.text
                            break
                    if latest_user_text:
                        break
            # Pass prompt by NAME rather than positionally so test stubs
            # whose stream() signature is ``stream(messages, **kwargs)``
            # absorb ``prompt`` via **kwargs without a positional clash.
            # Real backends declare ``stream(prompt, **kwargs)`` and pick
            # it up by name correctly.
            async for chunk in self._backend.stream(
                prompt=latest_user_text, messages=messages, **bk_kwargs
            ):
                if self._cancel_event.is_set():
                    break

                kind = chunk.kind
                if kind == ChunkKind.TEXT_DELTA:
                    text_buf.append(chunk.text)
                    yield AgentEvent(
                        kind=AgentEventKind.TEXT_DELTA,
                        turn=turn,
                        text=chunk.text,
                    )
                    # Fire text-delta observers (predictive cache et al.).
                    # Observers are awaited but should be lightweight — they
                    # typically schedule fire-and-forget tasks rather than
                    # block on real work.
                    for obs in self._text_delta_observers:
                        try:
                            await obs(chunk.text)
                        except Exception:
                            logger.exception("text_delta_observer raised — swallowing")
                elif kind == ChunkKind.THINKING_DELTA:
                    # Forward thinking-delta chunks as their own AgentEvent
                    # so the renderer can show the "thinking..." preview.
                    # Without this, extended-thinking output silently
                    # disappears under v2.
                    yield AgentEvent(
                        kind=AgentEventKind.THINKING_DELTA,
                        turn=turn,
                        text=chunk.text,
                        raw=getattr(chunk, "raw", None),
                    )
                elif kind == ChunkKind.MESSAGE_START:
                    # MESSAGE_START is informational here — we already fired
                    # TURN_START at the top of the loop. Some backends emit
                    # it; others don't. Either way, no extra event needed.
                    pass
                elif kind == ChunkKind.ERROR:
                    yield AgentEvent(
                        kind=AgentEventKind.ERROR,
                        turn=turn,
                        text=chunk.text or "stream error",
                    )
                elif kind == ChunkKind.TOOL_USE_START:
                    partial_names[chunk.tool_use_id] = chunk.tool_name
                    partial_inputs[chunk.tool_use_id] = []
                elif kind == ChunkKind.TOOL_USE_DELTA:
                    if chunk.tool_use_id in partial_inputs:
                        partial_inputs[chunk.tool_use_id].append(chunk.tool_input_delta)
                elif kind == ChunkKind.TOOL_USE_END:
                    tool_use_id = chunk.tool_use_id
                    name = partial_names.get(tool_use_id, chunk.tool_name)
                    raw = "".join(partial_inputs.get(tool_use_id, []))
                    try:
                        parsed_input = json.loads(raw) if raw else {}
                    except json.JSONDecodeError:
                        logger.warning(
                            "agent_loop_v2: malformed tool_use input for %s — using empty dict",
                            name,
                        )
                        parsed_input = {}
                    tool_calls.append(
                        ToolCallInfo(
                            tool_use_id=tool_use_id,
                            name=name,
                            input=parsed_input,
                        )
                    )
                # ChunkKind.DONE / TOOL_RESULT (echoes from backend): ignore.
                # We compute our own DONE/TOOL_RESULT from local state.

            full_text = "".join(text_buf)

            # Append assistant turn to history.
            assistant_blocks: list[ContentBlock] = []
            if full_text:
                assistant_blocks.append(ContentBlock(kind="text", text=full_text))
            for tc in tool_calls:
                assistant_blocks.append(
                    ContentBlock(
                        kind="tool_use",
                        tool_use_id=tc.tool_use_id,
                        tool_name=tc.name,
                        tool_input=tc.input,
                    )
                )
            messages.append(Message(role=Role.ASSISTANT, content=assistant_blocks))

            # Done if no tool calls.
            if not tool_calls:
                yield AgentEvent(
                    kind=AgentEventKind.AGENT_DONE,
                    turn=turn,
                    text=full_text,
                )
                return

            # Build the merged turn DAG. parallel_plan calls expand into
            # their declared sub-DAG; non-plan calls become no-edge sibling
            # nodes; both classes coexist in one TurnDAG.
            dag_ctx = self._build_dag(tool_calls)
            dag = dag_ctx.dag

            # Emit a TOOL_CALL event per node (in submission order) before
            # dispatch — callers logging or rendering UI need this.
            for node in sorted(dag, key=lambda n: n.submission_index):
                yield AgentEvent(
                    kind=AgentEventKind.TOOL_CALL,
                    turn=turn,
                    tool_name=node.tool_name,
                    tool_input=node.tool_input,
                    # Use the originating SDK tool_use_id (siblings = self;
                    # plan-children = parent plan) so consumers can correlate
                    # node-level events back to the assistant turn.
                    tool_use_id=dag_ctx.node_origins[node.id],
                )

            # Execute via the scheduler. Mode: parallel iff any node has
            # declared deps (or the merge produced sibling+plan nodes).
            envelopes_by_id: dict[str, _ToolEnvelopeV2] = {}

            # Per-turn ToolContext (built earlier at turn start) is
            # bound around every tool dispatch so tools that read
            # current_tool_context() see host_callbacks, the live
            # history, session_id, and the registry. v1 parity.
            tool_ctx = turn_tool_ctx

            async def _core_executor(
                node: DAGNode, _resolved: dict[str, Any]
            ) -> list[ContentBlock]:
                # seen_calls dedup: load-bearing for correctness on stream
                # retries. Skip when tool_use_id is empty — synthesized
                # parallel_plan children have no SDK identity.
                #
                # Note: side-table population (envelopes_by_id) happens
                # POST-middleware in the result-iteration loop below, so
                # middleware's return value is what reaches the SDK. That
                # makes capability_gate, tool_output_level, etc. actually
                # affect the user-turn content.
                tu_id = node.tool_use_id
                if tu_id and tu_id in self._seen_calls:
                    cached = self._seen_calls[tu_id]
                    return cached.content

                env = await self._dispatch_node(node, tool_ctx)
                if tu_id:
                    self._seen_calls[tu_id] = env
                return env.content

            # Compose the dispatch chain: middleware applied outermost-first.
            # The first entry in self._dispatch_middleware runs FIRST on the
            # way in (deepest in the call stack on the way out). Wrap inner
            # to outer by iterating in REVERSE.
            executor: Callable[..., Any] = _core_executor
            for mw in reversed(self._dispatch_middleware):
                executor = mw(executor)

            mode = "parallel" if any(n.depends_on for n in dag) else "sequential"
            scheduler = Scheduler(
                registry=self._registry,
                mode=mode,
                max_concurrency=self._config.max_concurrency,
                per_tool_concurrency=self._config.per_tool_concurrency,
                per_capability_concurrency=self._config.per_capability_concurrency,
                cancel_event=self._cancel_event,
                executor=executor,
            )

            results: list[DAGNodeResult] = []
            async for result in scheduler.run(dag, ctx=None):
                results.append(result)
                # Build the envelope from the *post-middleware* result. Any
                # ContentBlock with is_error=True propagates to the envelope
                # and the aggregated tool_result block, so capability_gate /
                # tool_output_level / others affect the user-turn payload.
                content_has_error = any(
                    getattr(b, "is_error", False) for b in result.content
                )
                env = _ToolEnvelopeV2(
                    tool_use_id=result.tool_use_id,
                    content=result.content,
                    is_error=content_has_error or not result.success,
                    is_cancelled=result.is_cancelled,
                )
                envelopes_by_id[result.node_id] = env
                origin_tu_id = dag_ctx.node_origins.get(
                    result.node_id, result.tool_use_id
                )
                yield AgentEvent(
                    kind=AgentEventKind.TOOL_RESULT,
                    turn=turn,
                    tool_name=dag.get(result.node_id).tool_name
                    if result.node_id in dag
                    else "",
                    tool_use_id=origin_tu_id,
                    tool_result=_envelope_to_text(env),
                )

            # Build user turn — ONE tool_result per SDK tool_use_id (the
            # SDK contract). Plan-expanded nodes aggregate into the parent
            # plan's tool_result; sibling nodes get their own.
            user_blocks = self._aggregate_results_to_user_blocks(
                dag_ctx, envelopes_by_id
            )
            messages.append(Message(role=Role.USER, content=user_blocks))

            # post_turn hook — invoked after tools complete and before the
            # next stream. Hook may set turn_ctx.stop_after_turn = True
            # (e.g. arbiter kill) to terminate the loop. Hook may also
            # mutate messages (rare — usually pre_turn handles that).
            if self._post_turn is not None:
                turn_result = TurnResult(
                    turn_index=turn,
                    text=full_text,
                    tool_calls=tuple(tool_calls),
                    results=tuple(results),
                )
                await self._post_turn(turn_ctx, turn_result)
                messages = turn_ctx.messages
                if turn_ctx.stop_after_turn:
                    yield AgentEvent(
                        kind=AgentEventKind.AGENT_DONE,
                        turn=turn,
                        text="stopped by post_turn hook",
                    )
                    return

        # max_turns exceeded.
        yield AgentEvent(
            kind=AgentEventKind.AGENT_DONE,
            turn=self._config.max_turns,
            text=f"max_turns ({self._config.max_turns}) reached",
        )

    # -- Helpers ---------------------------------------------------------------

    def _build_dag(self, tool_calls: list[ToolCallInfo]) -> _TurnDAGContext:
        """Build a merged TurnDAG from this turn's tool calls (Option B).

        For each tool_call:

        - ``parallel_plan`` calls are expanded into their declared DAG nodes.
          Each expanded node carries an empty SDK ``tool_use_id`` (it has
          no SDK identity — only the parent plan call does). Plan node ids
          come from the model's plan; we namespace by prefixing with the
          plan's tool_use_id to avoid collisions across multiple plans in
          the same turn.
        - Non-plan tool_uses become single no-edge sibling nodes.

        All nodes go into one merged :class:`TurnDAG`. Plan-internal
        dependencies are honored. Sibling nodes have no declared edges by
        default — they run in parallel with plan no-dep nodes. Set
        ``AgentLoopV2Config.siblings_wait_for_plan`` to inject edges from
        every sibling to every plan terminal node.

        Returns a context bundle so the caller can build per-SDK-tool_use
        result envelopes (each plan call → ONE aggregated tool_result;
        each sibling → its own tool_result).

        If a ``parallel_plan`` call has invalid input, that single call
        falls back to a sibling node (its handler will echo the input).
        Other plans in the same turn keep their expansions.
        """
        all_nodes: list[DAGNode] = []
        # node.id (in the merged DAG) -> originating SDK tool_use_id.
        # Plan-expanded nodes map back to the parent plan's SDK id; siblings
        # map to themselves. Used during result building to aggregate.
        node_origins: dict[str, str] = {}
        # Ordered list of SDK tool_use_ids we owe a tool_result envelope for.
        sdk_tool_use_ids: list[str] = []
        # Plan-id -> set of node.ids that are terminals of that plan's
        # internal sub-DAG. Used by siblings_wait_for_plan.
        plan_terminals: list[str] = []

        submission_idx = 0
        for tc in tool_calls:
            sdk_tool_use_ids.append(tc.tool_use_id)

            if tc.name == self._config.parallel_plan_tool_name:
                expanded = self._expand_plan_or_fallback(
                    tc, submission_idx_offset=submission_idx
                )
                if expanded is None:
                    # Fall back: treat invalid plan as a sibling node.
                    sibling = DAGNode(
                        id=tc.tool_use_id,
                        tool_name=tc.name,
                        tool_input=dict(tc.input),
                        depends_on=(),
                        submission_index=submission_idx,
                        tool_use_id=tc.tool_use_id,
                    )
                    all_nodes.append(sibling)
                    node_origins[sibling.id] = tc.tool_use_id
                    submission_idx += 1
                    continue

                # Successful expansion. Namespace plan node ids by prefixing
                # with the plan's SDK tool_use_id so multiple plans in the
                # same turn can't collide. Re-thread depends_on through the
                # same prefix.
                prefix = (
                    f"{tc.tool_use_id}:" if tc.tool_use_id else f"plan{submission_idx}:"
                )
                # Compute terminals BEFORE renaming so we can rename them too.
                internal_ids = {n.id for n in expanded}
                referenced = {dep for n in expanded for dep in n.depends_on}
                terminals_unprefixed = internal_ids - referenced

                for n in expanded:
                    new_id = f"{prefix}{n.id}"
                    new_deps = tuple(f"{prefix}{d}" for d in n.depends_on)
                    renamed = DAGNode(
                        id=new_id,
                        tool_name=n.tool_name,
                        tool_input=dict(n.tool_input),
                        depends_on=new_deps,
                        submission_index=n.submission_index,
                        tool_use_id="",  # synthesized — no SDK identity
                    )
                    all_nodes.append(renamed)
                    node_origins[new_id] = tc.tool_use_id
                    submission_idx += 1
                plan_terminals.extend(f"{prefix}{tid}" for tid in terminals_unprefixed)
            else:
                sibling = DAGNode(
                    id=tc.tool_use_id,
                    tool_name=tc.name,
                    tool_input=dict(tc.input),
                    depends_on=(),
                    submission_index=submission_idx,
                    tool_use_id=tc.tool_use_id,
                )
                all_nodes.append(sibling)
                node_origins[sibling.id] = tc.tool_use_id
                submission_idx += 1

        # If siblings_wait_for_plan and we have any plan terminals, rewrite
        # sibling nodes to depend on every terminal. Sibling = node whose
        # origin is its own id (no plan expansion produced it).
        if self._config.siblings_wait_for_plan and plan_terminals:
            terminal_tuple = tuple(plan_terminals)
            rebuilt: list[DAGNode] = []
            for n in all_nodes:
                origin = node_origins[n.id]
                # A node is a sibling iff its node.id equals its origin
                # (i.e., it wasn't synthesized from a parallel_plan).
                is_sibling = n.id == origin
                if is_sibling and not n.depends_on:
                    rebuilt.append(
                        DAGNode(
                            id=n.id,
                            tool_name=n.tool_name,
                            tool_input=n.tool_input,
                            depends_on=terminal_tuple,
                            submission_index=n.submission_index,
                            tool_use_id=n.tool_use_id,
                        )
                    )
                else:
                    rebuilt.append(n)
            all_nodes = rebuilt

        dag = TurnDAG(nodes=tuple(all_nodes))
        return _TurnDAGContext(
            dag=dag,
            node_origins=node_origins,
            sdk_tool_use_ids=tuple(sdk_tool_use_ids),
        )

    def _expand_plan_or_fallback(
        self,
        tc: ToolCallInfo,
        *,
        submission_idx_offset: int,
    ) -> tuple[DAGNode, ...] | None:
        """Try to parse + expand a parallel_plan call. Return None on failure
        so the caller can fall back to treating it as a sibling node."""
        try:
            parsed = parse_parallel_plan_input(tc.input)
        except ParallelPlanValidationError as exc:
            logger.warning(
                "parallel_plan input invalid (%s); falling back to sibling node",
                exc,
            )
            return None
        plan_dag = build_turn_dag_from_parallel_plan(
            parsed, submission_index_offset=submission_idx_offset
        )
        return tuple(plan_dag.nodes_in_topological_order())

    def _aggregate_results_to_user_blocks(
        self,
        dag_ctx: _TurnDAGContext,
        envelopes_by_id: dict[str, _ToolEnvelopeV2],
    ) -> list[ContentBlock]:
        """Build the next user turn's content blocks — exactly one
        tool_result per SDK tool_use_id, in submission order.

        Plan-expanded nodes (whose origin maps to the plan's SDK tool_use_id)
        are aggregated into a single tool_result block keyed by that id.
        Each plan node's per-node result is included in the aggregation
        prefixed with its node id and tool name, so the model can correlate.

        Sibling nodes (origin == self) emit their own tool_result block
        directly.
        """
        # Group nodes by originating SDK tool_use_id, preserving submission order.
        nodes_by_origin: dict[str, list[DAGNode]] = {}
        for node in sorted(dag_ctx.dag, key=lambda n: n.submission_index):
            origin = dag_ctx.node_origins[node.id]
            nodes_by_origin.setdefault(origin, []).append(node)

        blocks: list[ContentBlock] = []
        for sdk_tu_id in dag_ctx.sdk_tool_use_ids:
            nodes = nodes_by_origin.get(sdk_tu_id, [])
            if not nodes:
                # No node landed for this tool_use — synthesize a missing-result
                # error so the SDK contract isn't violated.
                blocks.append(
                    ContentBlock(
                        kind="tool_result",
                        tool_use_id=sdk_tu_id,
                        text="(no result)",
                        is_error=True,
                    )
                )
                continue

            if len(nodes) == 1:
                # Sibling or single-node plan — pass through.
                env = envelopes_by_id.get(nodes[0].id)
                if env is None:
                    blocks.append(
                        ContentBlock(
                            kind="tool_result",
                            tool_use_id=sdk_tu_id,
                            text="(no result)",
                            is_error=True,
                        )
                    )
                else:
                    blocks.append(
                        ContentBlock(
                            kind="tool_result",
                            tool_use_id=sdk_tu_id,
                            text=_envelope_to_text(env),
                            is_error=env.is_error,
                        )
                    )
                continue

            # Multi-node plan — aggregate into one tool_result block.
            parts: list[str] = []
            any_error = False
            for n in nodes:
                env = envelopes_by_id.get(n.id)
                if env is None:
                    parts.append(f"[{n.id}] (no result)")
                    any_error = True
                    continue
                if env.is_error:
                    any_error = True
                parts.append(f"[{n.id} {n.tool_name}] {_envelope_to_text(env)}")
            blocks.append(
                ContentBlock(
                    kind="tool_result",
                    tool_use_id=sdk_tu_id,
                    text="\n".join(parts),
                    is_error=any_error,
                )
            )

        return blocks

    async def _dispatch_node(
        self,
        node: DAGNode,
        tool_ctx: ToolContext,
    ) -> _ToolEnvelopeV2:
        """Look up the tool, invoke it, wrap the result in a v2 envelope.

        Binds *tool_ctx* via :func:`bind_tool_context` for the duration of
        the handler call so tools that read ``current_tool_context()`` see
        host_callbacks, session_id, history, registry, etc. — v1 parity.

        v2 deliberately keeps this minimal — capability checks, hooks,
        predictive cache, etc. are middleware concerns. They wrap this
        method or the scheduler.executor. Today: lookup → bind → invoke
        → wrap. Errors during invocation become an error envelope.
        """
        spec: ToolSpec | None = self._registry.get(node.tool_name)
        if spec is None:
            return _ToolEnvelopeV2(
                tool_use_id=node.tool_use_id,
                content=[
                    ContentBlock(
                        kind="text",
                        text=f"tool not found: {node.tool_name}",
                    ),
                ],
                is_error=True,
            )

        started = time.monotonic()
        try:
            handler = spec.handler
            with bind_tool_context(tool_ctx):
                result = handler(**node.tool_input)
                if asyncio.iscoroutine(result):
                    result = await result
        except Exception as exc:  # noqa: BLE001 — surface to model
            logger.exception("agent_loop_v2: tool %s raised", node.tool_name)
            return _ToolEnvelopeV2(
                tool_use_id=node.tool_use_id,
                content=[
                    ContentBlock(
                        kind="text",
                        text=f"{type(exc).__name__}: {exc}",
                    ),
                ],
                is_error=True,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        text = result if isinstance(result, str) else json.dumps(result, default=str)
        return _ToolEnvelopeV2(
            tool_use_id=node.tool_use_id,
            content=[ContentBlock(kind="text", text=text)],
            is_error=False,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _build_tool_context(
        self,
        *,
        messages: list[Message],
        session_id: str,
    ) -> ToolContext:
        """Build a per-turn ToolContext with registry + history + host_callbacks.

        host_callbacks dict keys are mapped to ToolContext fields:
        ``ask_user_callback``, ``user_interact_callback``,
        ``permission_mode_callback``, ``plan_approval_callback``,
        ``mcp_discovery_report``. Unknown keys go into ``extras``.
        """
        cb = self._host_callbacks
        return ToolContext(
            registry=self._registry,
            history=messages,  # live mutable ref — same object the loop appends to
            session_id=session_id,
            ask_user_callback=cb.get("ask_user_callback"),
            user_interact_callback=cb.get("user_interact_callback"),
            permission_mode_callback=cb.get("permission_mode_callback"),
            plan_approval_callback=cb.get("plan_approval_callback"),
            mcp_discovery_report=cb.get("mcp_discovery_report"),
            extras={k: v for k, v in cb.items() if k not in _TOOL_CTX_KNOWN_FIELDS},
        )


# ---------------------------------------------------------------------------
# Internal envelope (lighter than ToolResultEnvelope; less coupled to v1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ToolEnvelopeV2:
    """v2-internal tool result. Purposely simpler than ``ToolResultEnvelope``.

    The bridge to the v1 ``ToolResultEnvelope`` lives in callers that need
    cross-version compatibility — v2 itself doesn't depend on it.
    """

    tool_use_id: str
    content: list[ContentBlock]
    is_error: bool = False
    is_cancelled: bool = False
    latency_ms: int = 0


@dataclass(frozen=True)
class _TurnDAGContext:
    """Bundle returned by :meth:`AgentLoopV2._build_dag`.

    Carries the merged DAG plus the side-tables the executor needs to
    correlate node results back to SDK tool_uses for the next user turn.
    """

    dag: TurnDAG
    # node.id -> originating SDK tool_use_id. Plan-expanded nodes map to
    # the parent plan's SDK id; sibling nodes map to themselves.
    node_origins: dict[str, str]
    # Original SDK tool_use_ids in the order the model emitted them. The
    # next user turn must contain exactly one tool_result per id, in
    # this order, with the matching ids — SDK contract.
    sdk_tool_use_ids: tuple[str, ...]


def _envelope_to_text(env: _ToolEnvelopeV2) -> str:
    """Concatenate text-bearing blocks for backwards-compatible event payloads.

    Reads ``b.text`` from any block that has a non-empty ``text`` attribute
    (TextBlock, ThinkingBlock) AND the ``content`` field of ToolResultBlock
    (which holds the displayable text for tool results). Skips blocks with
    no string body.
    """
    parts: list[str] = []
    for b in env.content:
        text = getattr(b, "text", "")
        if not text:
            # ToolResultBlock has ``content``, not ``text`` — fall back.
            content = getattr(b, "content", "")
            if isinstance(content, str):
                text = content
        if text:
            parts.append(text)
    return "\n".join(parts)
