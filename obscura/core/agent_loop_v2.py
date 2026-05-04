"""obscura.core.agent_loop_v2 — DAG-native agent loop (clean rewrite).

This is the successor to :class:`obscura.core.agent_loop.AgentLoop` (v1).
v1 is ~10K lines and grew organically: a streaming tool executor, predictive
cache, capability gates, retries, hooks, arbiter integration, and the
core turn loop are all interleaved. v2 separates the core loop from the
optional behaviors and uses :mod:`obscura.core.dag` natively for tool
execution.

Architectural diff from v1
==========================

+--------------------------+--------------------------------+--------------------------------+
| Concern                  | v1                             | v2                             |
+==========================+================================+================================+
| Tool dispatch            | StreamingToolExecutor          | dag.Scheduler                  |
| Intra-turn parallelism   | side_effects=="none" only      | DAG edges + concurrency caps   |
| Retries / backoff        | inline in run()                | (TODO — middleware)            |
| Predictive cache         | inline in run()                | (TODO — middleware)            |
| Capability gates         | inline _execute_single_tool    | (TODO — middleware)            |
| Arbiter                  | inline                         | (TODO — middleware)            |
| seen_calls dedup         | StreamingToolExecutor.seen_calls| _seen_calls dict + check at   |
|                          |                                | dispatch (load-bearing)        |
| Cancellation             | abort_event + task.cancel      | scheduler.cancel_event         |
| Compaction               | inline                         | hook callbacks                 |
+--------------------------+--------------------------------+--------------------------------+

What v2 owns
------------

A focused, ~400-line implementation that:

1. Streams from a :class:`BackendProtocol`, one turn at a time.
2. Collects ``tool_use`` blocks during the stream into ``ToolCallInfo`` objects.
3. After the assistant turn ends, builds a :class:`TurnDAG` from the collected
   calls (with no edges by default — matches today's batch behavior).
4. Runs the DAG through :class:`Scheduler`, sequential or parallel depending
   on whether any node has declared ``depends_on``.
5. Yields :class:`AgentEvent` instances throughout (TEXT_DELTA, TOOL_CALL,
   TOOL_RESULT, AGENT_DONE) — same shape as v1 so callers don't need
   to change.
6. Repeats until the model emits no tool calls or ``max_turns`` is exceeded.

What v2 deliberately leaves out
-------------------------------

* Capability tokens, tool allowlists — middleware concern, not core.
* Predictive cache — speculative-fetch is a separate optimization layer.
* Arbiter integration — wraps over v2 via hooks.
* Per-turn retry loop with timeouts — caller wraps with ``asyncio.timeout``.
* Tool confirmation prompts — caller injects via hooks.

These all worked fine in v1 — they just made v1 hard to read. v2 keeps
the core loop legible; advanced behaviors compose on top.

Migration story
---------------

v1 is not deleted. Both classes coexist:

* New code starts on :class:`AgentLoopV2`.
* Existing callers stay on :class:`obscura.core.agent_loop.AgentLoop` until
  the eval harness shows v2 reaches feature parity for their workflows.
* Stage D in the surface-split plan removes v1 once parity is real.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
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
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._config = config or AgentLoopV2Config()
        self._cancel_event = cancel_event or asyncio.Event()

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
    ) -> AsyncIterator[AgentEvent]:
        """Drive the agent until the model emits no tool calls or ``max_turns``."""
        session_id = session_id or str(uuid.uuid4())
        messages: list[Message] = list(history or [])
        messages.append(
            Message(role=Role.USER, content=[ContentBlock(kind="text", text=prompt)])
        )

        async for event in self._run_inner(messages, session_id):
            yield event

    # -- Internal --------------------------------------------------------------

    async def _run_inner(
        self,
        messages: list[Message],
        session_id: str,
    ) -> AsyncIterator[AgentEvent]:
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

            # Stream the next assistant turn.
            text_buf: list[str] = []
            tool_calls: list[ToolCallInfo] = []
            partial_inputs: dict[
                str, list[str]
            ] = {}  # tool_use_id -> JSON delta chunks
            partial_names: dict[str, str] = {}

            async for chunk in self._backend.stream(messages=messages):
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
                # MESSAGE_END / other kinds: ignore.

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

            # Build the turn DAG. parallel_plan calls expand into the DAG
            # natively; everything else becomes a no-edge node.
            dag = self._build_dag(tool_calls)

            # Emit a TOOL_CALL event per node (in submission order) before
            # dispatch — callers logging or rendering UI need this.
            for node in sorted(dag, key=lambda n: n.submission_index):
                yield AgentEvent(
                    kind=AgentEventKind.TOOL_CALL,
                    turn=turn,
                    tool_name=node.tool_name,
                    tool_input=node.tool_input,
                    tool_use_id=node.tool_use_id,
                )

            # Execute via the scheduler. Mode: parallel iff any node has
            # declared deps (or there are >1 nodes — caller-determined cap
            # still serializes if max_concurrency=1).
            envelopes_by_id: dict[str, _ToolEnvelopeV2] = {}

            async def _node_executor(
                node: DAGNode, _resolved: dict[str, Any]
            ) -> list[ContentBlock]:
                # seen_calls dedup: load-bearing for correctness on stream
                # retries. Skip when tool_use_id is empty — synthesized
                # parallel_plan children have no SDK identity.
                tu_id = node.tool_use_id
                if tu_id and tu_id in self._seen_calls:
                    cached = self._seen_calls[tu_id]
                    envelopes_by_id[node.id] = cached
                    return cached.content

                env = await self._dispatch_node(node)
                if tu_id:
                    self._seen_calls[tu_id] = env
                envelopes_by_id[node.id] = env
                return env.content

            mode = "parallel" if any(n.depends_on for n in dag) else "sequential"
            scheduler = Scheduler(
                registry=self._registry,
                mode=mode,
                max_concurrency=self._config.max_concurrency,
                per_tool_concurrency=self._config.per_tool_concurrency,
                per_capability_concurrency=self._config.per_capability_concurrency,
                cancel_event=self._cancel_event,
                executor=_node_executor,
            )

            results: list[DAGNodeResult] = []
            async for result in scheduler.run(dag, ctx=None):
                results.append(result)
                env = envelopes_by_id.get(result.node_id)
                if env is None:
                    # Node was synthesized cancelled — build envelope from result.
                    env = _ToolEnvelopeV2(
                        tool_use_id=result.tool_use_id,
                        content=result.content,
                        is_error=not result.success,
                        is_cancelled=result.is_cancelled,
                    )
                    envelopes_by_id[result.node_id] = env
                yield AgentEvent(
                    kind=AgentEventKind.TOOL_RESULT,
                    turn=turn,
                    tool_name=dag.get(result.node_id).tool_name
                    if result.node_id in dag
                    else "",
                    tool_use_id=result.tool_use_id,
                    tool_result=_envelope_to_text(env),
                )

            # Build user turn from envelopes in SUBMISSION order. The SDK
            # contract requires every tool_use to have a matching tool_result
            # in the next user message (cancelled nodes get an is_error
            # envelope above).
            user_blocks: list[ContentBlock] = []
            for node in sorted(dag, key=lambda n: n.submission_index):
                env = envelopes_by_id.get(node.id)
                if env is None:
                    env = _ToolEnvelopeV2(
                        tool_use_id=node.tool_use_id,
                        content=[ContentBlock(kind="text", text="(no result)")],
                        is_error=True,
                    )
                user_blocks.append(
                    ContentBlock(
                        kind="tool_result",
                        tool_use_id=env.tool_use_id,
                        text=_envelope_to_text(env),
                        is_error=env.is_error,
                    )
                )
            messages.append(Message(role=Role.USER, content=user_blocks))

        # max_turns exceeded.
        yield AgentEvent(
            kind=AgentEventKind.AGENT_DONE,
            turn=self._config.max_turns,
            text=f"max_turns ({self._config.max_turns}) reached",
        )

    # -- Helpers ---------------------------------------------------------------

    def _build_dag(self, tool_calls: list[ToolCallInfo]) -> TurnDAG:
        """Build a TurnDAG from this turn's tool calls.

        If exactly one ``parallel_plan`` call is present, expand it into the
        DAG. Otherwise build a no-edge DAG (each call is an isolated node)
        — matches v1 batch behavior.

        Mixed cases (parallel_plan + other calls in the same turn) are not
        supported in v2 yet; we treat the mix as no-edge and log a warning,
        falling back to the safer of the two semantics.
        """
        plan_calls = [
            tc for tc in tool_calls if tc.name == self._config.parallel_plan_tool_name
        ]
        if len(plan_calls) == 1 and len(tool_calls) == 1:
            try:
                parsed = parse_parallel_plan_input(plan_calls[0].input)
            except ParallelPlanValidationError as exc:
                logger.warning(
                    "parallel_plan input invalid (%s); falling back to no-edge", exc
                )
                return self._no_edge_dag(tool_calls)
            return build_turn_dag_from_parallel_plan(parsed)

        if plan_calls:
            logger.warning(
                "agent_loop_v2: parallel_plan mixed with other tool_use blocks — "
                "treating as no-edge DAG (mixed mode is not supported yet)"
            )
        return self._no_edge_dag(tool_calls)

    @staticmethod
    def _no_edge_dag(tool_calls: list[ToolCallInfo]) -> TurnDAG:
        nodes = tuple(
            DAGNode(
                id=tc.tool_use_id,
                tool_name=tc.name,
                tool_input=dict(tc.input),
                depends_on=(),
                submission_index=i,
                tool_use_id=tc.tool_use_id,
            )
            for i, tc in enumerate(tool_calls)
        )
        return TurnDAG(nodes=nodes)

    async def _dispatch_node(self, node: DAGNode) -> _ToolEnvelopeV2:
        """Look up the tool, invoke it, wrap the result in a v2 envelope.

        v2 deliberately keeps this minimal — capability checks, hooks,
        predictive cache, etc. are middleware concerns. They wrap this
        method or the scheduler.executor. Today: lookup → invoke →
        wrap. Errors during invocation become an error envelope.
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


def _envelope_to_text(env: _ToolEnvelopeV2) -> str:
    """Concatenate text blocks for backwards-compatible event payloads."""
    parts = [b.text for b in env.content if b.kind == "text" and b.text]
    return "\n".join(parts)
