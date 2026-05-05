"""obscura.core.dag — DAG types + Scheduler for tool-call execution.

Stage B1 of the agent-loop refactor. Defines the data types and execution
engine for a turn-scoped DAG of tool calls. The current
``StreamingToolExecutor`` in :mod:`obscura.core.agent_loop` runs tools in
*intra-batch* parallel groups (consecutive concurrency-safe tools batched
together) but knows nothing about *declared* dependencies between calls.
This module adds that concept.

Stage B1 lands the file but does **not** wire it into ``agent_loop.py`` —
that integration is Stage B2.

Two execution modes:

* ``"sequential"`` — iterates ``nodes_in_topological_order()`` and runs
  each node by itself. Bit-for-bit identical to today's no-edge sequential
  batch — used as the safe default until callers opt into parallel
  scheduling.
* ``"parallel"`` — respects ``depends_on`` edges, runs independent siblings
  concurrently (under a global concurrency cap, with optional per-tool and
  per-capability sub-caps). On a node failure, transitively cancels all
  descendants. On external cancel (``cancel_event.set()``), cancels every
  in-flight task and synthesizes a cancelled result for everything still
  pending — load-bearing for the SDK contract: every ``tool_use`` MUST
  have a matching ``tool_result`` envelope, otherwise the next model
  request errors out.

The :class:`Scheduler.run` async iterator yields :class:`DAGNodeResult`
values. Sequential mode yields in topological order; parallel mode yields
in completion order — the caller is responsible for re-sorting by
``submission_index`` if SDK ordering matters (and it usually does for
multi-turn message bookkeeping).

Argument resolution: tool inputs may contain ``${node_id.path}``
placeholders that reference upstream node results. See
:func:`resolve_args` for the supported shape.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from obscura.core.types import ContentBlock

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from obscura.core.tools import ToolRegistry

__all__ = [
    "DAGArgResolutionError",
    "DAGError",
    "DAGExecutionError",
    "DAGNode",
    "DAGNodeResult",
    "DAGValidationError",
    "Scheduler",
    "TurnDAG",
    "resolve_args",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DAGError(Exception):
    """Base class for DAG-related errors."""


class DAGValidationError(DAGError):
    """Raised when a :class:`TurnDAG` fails validation.

    Triggers: cycles, missing ``depends_on`` ids, duplicate node ids.
    """


class DAGArgResolutionError(DAGError):
    """Raised when a ``${node_id.path}`` placeholder cannot be resolved.

    Triggers: referenced node failed or was cancelled, referenced node
    exists but the requested path does not, type mismatch on traversal.
    """


class DAGExecutionError(DAGError):
    """Raised when the scheduler hits an unrecoverable runtime error.

    Distinct from per-node failures (those flow through
    :class:`DAGNodeResult` with ``success=False``). This signals a bug in
    the scheduler itself or the host registry, not a bad tool call.
    """


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

# DAG nodes carry the heterogeneous JSON arg shape of arbitrary tools — the
# DAG framework is generic across every registered ToolSpec, so tool_input
# is legitimately ``dict[str, Any]`` here. Per-tool typing happens inside
# each tool handler, not at the DAG layer.


def _empty_str_any_dict() -> dict[str, Any]:
    return {}


@dataclass(frozen=True)
class DAGNode:
    """One node in a turn DAG — a single tool call.

    Attributes
    ----------
    id
        Unique within the turn. Used as the key in ``depends_on`` edges
        and ``${id.path}`` placeholders.
    tool_name
        The tool to invoke. Looked up via ``ToolRegistry.get(name)``.
    tool_input
        Arguments for the tool. May contain ``${node_id.path}`` strings
        that get resolved against completed upstream results at dispatch.
    depends_on
        Tuple of upstream node ids. The scheduler will not start this
        node until every id in this tuple has completed successfully.
    submission_index
        Original order from the model output. Used to re-sort results
        when the caller needs SDK-ordered output (every ``tool_use``
        must pair with a matching ``tool_result`` in order).
    tool_use_id
        The SDK-level tool_use_id — opaque token from the provider
        SDK that pairs ``tool_use`` to ``tool_result``. Threaded
        through unchanged so the result envelope can carry it.
    """

    id: str
    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=_empty_str_any_dict)
    depends_on: tuple[str, ...] = ()
    submission_index: int = 0
    tool_use_id: str = ""


@dataclass(frozen=True)
class DAGNodeResult:
    """Outcome of executing one :class:`DAGNode`.

    Failure modes are explicit: ``success=False`` covers tool errors
    raised at dispatch, ``is_cancelled=True`` covers external cancel
    + upstream-failure cascade. Callers building ``tool_result``
    envelopes for the next model turn should treat both as errors.
    """

    node_id: str
    tool_use_id: str
    submission_index: int
    success: bool
    content: list[ContentBlock]
    error: str | None = None
    is_cancelled: bool = False
    started_at: float = 0.0
    completed_at: float = 0.0


# ---------------------------------------------------------------------------
# TurnDAG
# ---------------------------------------------------------------------------


@dataclass
class TurnDAG:
    """A validated DAG of tool calls scoped to a single agent turn.

    Construct with ``TurnDAG(nodes=[...])``. ``__post_init__`` validates
    the graph and raises :class:`DAGValidationError` if it's malformed —
    so callers can construct-and-go without a separate ``.validate()``
    step.
    """

    nodes: list[DAGNode] = field(default_factory=list[DAGNode])

    # Computed in __post_init__. Marked private but exposed via methods.
    _by_id: dict[str, DAGNode] = field(default_factory=dict[str, DAGNode], init=False)
    _topo: list[str] = field(default_factory=list[str], init=False)
    _children: dict[str, set[str]] = field(
        default_factory=dict[str, set[str]], init=False
    )

    def __post_init__(self) -> None:
        self._validate_and_index()

    def _validate_and_index(self) -> None:
        # Check duplicate ids first — clearer error than a topo cycle.
        seen: set[str] = set()
        for node in self.nodes:
            if node.id in seen:
                raise DAGValidationError(f"Duplicate node id: {node.id!r}")
            seen.add(node.id)
        self._by_id = {node.id: node for node in self.nodes}

        # Validate every depends_on edge points at a real node.
        for node in self.nodes:
            for dep in node.depends_on:
                if dep not in self._by_id:
                    raise DAGValidationError(
                        f"Node {node.id!r} depends on unknown node {dep!r}"
                    )
                if dep == node.id:
                    raise DAGValidationError(
                        f"Node {node.id!r} cannot depend on itself"
                    )

        # Build child map for descendants_of.
        children: dict[str, set[str]] = {nid: set() for nid in self._by_id}
        for node in self.nodes:
            for dep in node.depends_on:
                children[dep].add(node.id)
        self._children = children

        # Kahn's algorithm — both validates acyclicity and produces topo order.
        in_degree: dict[str, int] = {
            nid: len(node.depends_on) for nid, node in self._by_id.items()
        }
        # Use submission_index to break ties — keeps topo stable across runs.
        ready: deque[str] = deque(
            sorted(
                (nid for nid, deg in in_degree.items() if deg == 0),
                key=lambda nid: self._by_id[nid].submission_index,
            ),
        )
        order: list[str] = []
        while ready:
            nid = ready.popleft()
            order.append(nid)
            # Sort children by submission_index for stable ordering.
            for child in sorted(
                children[nid],
                key=lambda c: self._by_id[c].submission_index,
            ):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    ready.append(child)
        if len(order) != len(self._by_id):
            unresolved = sorted(set(self._by_id) - set(order))
            raise DAGValidationError(
                f"Cycle detected in DAG; unresolved nodes: {unresolved}"
            )
        self._topo = order

    # -- Iteration / lookup ------------------------------------------------

    def __len__(self) -> int:
        return len(self.nodes)

    def __iter__(self) -> Iterator[DAGNode]:
        return iter(self.nodes)

    def __contains__(self, node_id: object) -> bool:
        return isinstance(node_id, str) and node_id in self._by_id

    def get(self, node_id: str) -> DAGNode:
        try:
            return self._by_id[node_id]
        except KeyError as exc:
            raise DAGValidationError(f"Unknown node id: {node_id!r}") from exc

    # -- Graph queries -----------------------------------------------------

    def nodes_in_topological_order(self) -> list[DAGNode]:
        """Return all nodes in a stable topological order.

        Stability rule: among nodes with no remaining unmet dependencies,
        the one with the smaller ``submission_index`` comes first. This
        makes the order match today's executor when the DAG has no edges.
        """
        return [self._by_id[nid] for nid in self._topo]

    def ready_nodes(self, completed: set[str]) -> list[DAGNode]:
        """Nodes whose every dependency is in *completed*.

        Excludes nodes already in *completed*. Returned in
        ``submission_index`` order so the scheduler dispatches them in a
        predictable sequence.
        """
        ready: list[DAGNode] = []
        for node in self.nodes:
            if node.id in completed:
                continue
            if all(dep in completed for dep in node.depends_on):
                ready.append(node)
        ready.sort(key=lambda n: n.submission_index)
        return ready

    def descendants_of(self, node_id: str) -> set[str]:
        """All nodes that transitively depend on *node_id* (excluding itself).

        Used for failure cascade: when a node fails, every descendant is
        cancelled because at least one of its dependencies will never
        complete successfully.
        """
        if node_id not in self._by_id:
            raise DAGValidationError(f"Unknown node id: {node_id!r}")
        result: set[str] = set()
        stack: list[str] = list(self._children.get(node_id, set()))
        while stack:
            child = stack.pop()
            if child in result:
                continue
            result.add(child)
            stack.extend(self._children.get(child, set()))
        return result


# ---------------------------------------------------------------------------
# Argument resolution
# ---------------------------------------------------------------------------

# ``${id.path.to.field}`` — id may be alphanumeric/underscore/dash, path
# segments dotted alphanumeric/underscore. We deliberately match the *whole*
# string (^…$) for substitution; substring matches are not supported.
_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Za-z0-9_\-]+)((?:\.[A-Za-z0-9_]+)*)\}$")


def _result_view(result: DAGNodeResult) -> dict[str, Any]:
    """Build the dict view of a result that placeholders traverse.

    Shape::

        {
            "content": [<ContentBlock>...],
            "text": "<concatenated text from text-kind blocks>",
            "error": <error string or None>,
            "node_id": "<id>",
            "tool_use_id": "<id>",
        }

    Tools that produce multiple text blocks see them concatenated under
    ``text``; richer paths can drill into ``content[N].field`` if needed.
    """
    text_parts = [b.text for b in result.content if b.kind == "text"]
    return {
        "content": list(result.content),
        "text": "".join(text_parts),
        "error": result.error,
        "node_id": result.node_id,
        "tool_use_id": result.tool_use_id,
    }


def _traverse(view: object, path: list[str], full_ref: str) -> Any:
    cur: object = view
    for segment in path:
        if isinstance(cur, dict):
            cur_dict: dict[str, Any] = cur  # type: ignore[assignment]
            if segment not in cur_dict:
                raise DAGArgResolutionError(
                    f"Path segment {segment!r} not found in {full_ref!r}"
                )
            cur = cur_dict[segment]
        elif isinstance(cur, (list, tuple)):
            cur_seq: list[Any] | tuple[Any, ...] = cur  # type: ignore[assignment]
            try:
                idx = int(segment)
            except ValueError as exc:
                raise DAGArgResolutionError(
                    f"Cannot index list with non-integer {segment!r} in {full_ref!r}"
                ) from exc
            try:
                cur = cur_seq[idx]
            except IndexError as exc:
                raise DAGArgResolutionError(
                    f"List index {idx} out of range in {full_ref!r}"
                ) from exc
        else:
            # Attribute access fallback for dataclasses (e.g. ContentBlock).
            if not hasattr(cur, segment):
                raise DAGArgResolutionError(
                    f"Cannot traverse {segment!r} on {type(cur).__name__} in {full_ref!r}"
                )
            cur = getattr(cur, segment)
    return cur


def _resolve_one(value: str, completed: dict[str, DAGNodeResult]) -> Any:
    match = _PLACEHOLDER_RE.match(value)
    if match is None:
        # Not a whole-string placeholder — pass through unchanged.
        # (Substring substitution is intentionally unsupported. A model
        # mixing literal text with placeholders should split into
        # multiple fields.)
        return value
    node_id = match.group(1)
    path_str = match.group(2) or ""
    path = [p for p in path_str.split(".") if p]

    if node_id not in completed:
        # Node referenced doesn't exist (yet). Leave the literal alone —
        # a typo or model error, surface to the tool which will probably
        # fail in a more useful way than a scheduler-level crash.
        return value

    result = completed[node_id]
    if not result.success or result.is_cancelled:
        raise DAGArgResolutionError(
            f"Cannot resolve {value!r}: node {node_id!r} did not succeed "
            f"(error: {result.error or 'cancelled'})"
        )

    view = _result_view(result)
    return _traverse(view, path, value)


def resolve_args(
    tool_input: dict[str, Any],
    completed: dict[str, DAGNodeResult],
) -> dict[str, Any]:
    """Walk *tool_input* and substitute any ``${node_id.path}`` placeholders.

    Supports a small, predictable substitution shape:

    * ``"${node1}"`` → entire ``_result_view(node1)`` dict
    * ``"${node1.text}"`` → concatenated text content of ``node1``
    * ``"${node1.content.0.text}"`` → first content block's text
    * ``"${node1.error}"`` → error string (None if successful)

    Rules:

    * Placeholders match only as a whole string value. Substring
      substitution (``"prefix-${node.text}-suffix"``) is **not**
      supported — split into separate fields if you need that.
    * Recurses into nested dicts and lists.
    * If the referenced node is missing, the literal is left untouched
      (likely a model typo — let the tool surface a useful error).
    * If the referenced node failed or was cancelled, raises
      :class:`DAGArgResolutionError` (the scheduler turns this into a
      synthesized failure result for the dependent node).

    Examples
    --------
    >>> result = DAGNodeResult(
    ...     node_id="n1", tool_use_id="t1", submission_index=0,
    ...     success=True, content=[ContentBlock(kind="text", text="hello")],
    ... )
    >>> resolve_args({"msg": "${n1.text}"}, {"n1": result})
    {'msg': 'hello'}
    """

    def _walk(node: object) -> Any:
        if isinstance(node, dict):
            node_dict: dict[Any, Any] = node  # type: ignore[assignment]
            return {k: _walk(v) for k, v in node_dict.items()}
        if isinstance(node, list):
            node_list: list[Any] = node  # type: ignore[assignment]
            return [_walk(v) for v in node_list]
        if isinstance(node, str):
            return _resolve_one(node, completed)
        return node

    walked = _walk(tool_input)
    if not isinstance(walked, dict):
        # _walk on a dict input always returns a dict; this branch is
        # unreachable but type-narrows for pyright.
        raise DAGExecutionError("resolve_args internal error: walked non-dict input")
    return cast("dict[str, Any]", walked)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


# Type alias for the per-node executor function. Exposed so tests and
# future Stage B2 wiring can override the default registry-backed handler.
NodeExecutor = Callable[[DAGNode, dict[str, Any]], Awaitable[list[ContentBlock]]]


class Scheduler:
    """Executes a :class:`TurnDAG` either sequentially or in parallel.

    The scheduler does *not* know about ``ToolResultEnvelope``,
    ``ToolCallInfo``, dedup caches, or any other agent-loop concept.
    Stage B2 will wrap this with the loop-specific glue. Here, a node is
    "execute the tool, get content blocks back, wrap in a
    :class:`DAGNodeResult`."

    Parameters
    ----------
    registry
        The tool registry. Looked up by tool name on each node dispatch.
    mode
        ``"sequential"`` for bit-for-bit-with-today behaviour,
        ``"parallel"`` for dependency-respecting concurrent execution.
    max_concurrency
        Global cap on simultaneously-running nodes in parallel mode.
    per_tool_concurrency
        Optional ``{tool_name: limit}`` map. A tool with a limit of 1
        runs effectively serially even if the global cap is higher.
    per_capability_concurrency
        Optional ``{capability: limit}`` map. Looked up via
        ``ToolSpec.capability``. Useful for rate-limiting an external
        service that several tool wrappers share.
    cancel_event
        External cancel signal. When set, the scheduler cancels every
        in-flight task and synthesizes ``is_cancelled=True`` results
        for everything still pending — load-bearing for the SDK
        contract that every tool_use needs a matching tool_result.
    executor
        Override for the per-node execution function. Default looks up
        the tool in ``registry`` and ``await``\\ s its handler with the
        resolved args. Tests pass a stub here so they don't have to
        register full ToolSpec machinery.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        mode: Literal["sequential", "parallel"] = "sequential",
        max_concurrency: int = 8,
        per_tool_concurrency: dict[str, int] | None = None,
        per_capability_concurrency: dict[str, int] | None = None,
        cancel_event: asyncio.Event | None = None,
        executor: NodeExecutor | None = None,
    ) -> None:
        self._registry = registry
        self._mode: Literal["sequential", "parallel"] = mode
        self._max_concurrency = max(1, max_concurrency)
        self._per_tool_concurrency: dict[str, int] = dict(per_tool_concurrency or {})
        self._per_capability_concurrency: dict[str, int] = dict(
            per_capability_concurrency or {}
        )
        self._cancel_event = cancel_event
        self._executor: NodeExecutor = executor or self._default_execute
        # Lazily built per-tool / per-capability semaphores.
        self._tool_semaphores: dict[str, asyncio.Semaphore] = {}
        self._capability_semaphores: dict[str, asyncio.Semaphore] = {}

    # -- Public --------------------------------------------------------------

    async def run(
        self,
        dag: TurnDAG,
        ctx: Any = None,
    ) -> AsyncIterator[DAGNodeResult]:
        """Execute *dag* under the scheduler's mode.

        Yields :class:`DAGNodeResult` values as nodes complete.

        * Sequential mode: yields in topological order, one node at a time.
        * Parallel mode: yields in completion order. Re-sort by
          ``submission_index`` if downstream needs SDK ordering.

        The *ctx* parameter is reserved for future use — currently passed
        through to the executor unchanged so a Stage B2 caller can thread
        a :class:`~obscura.core.tool_context.ToolContext` without
        changing this API.
        """
        if self._mode == "sequential":
            async for r in self._run_sequential(dag, ctx):
                yield r
        else:
            async for r in self._run_parallel(dag, ctx):
                yield r

    # -- Sequential mode -----------------------------------------------------

    async def _run_sequential(
        self,
        dag: TurnDAG,
        ctx: Any,
    ) -> AsyncIterator[DAGNodeResult]:
        completed: dict[str, DAGNodeResult] = {}
        cancelled_after_failure = False
        cancelled_external = False

        for node in dag.nodes_in_topological_order():
            # External cancel — synthesize cancelled results for the rest.
            if self._cancel_event is not None and self._cancel_event.is_set():
                cancelled_external = True

            if cancelled_external:
                yield self._synth_cancelled(node, "external cancel")
                continue

            # Failure cascade — if any of this node's deps failed, cancel.
            failed_dep = next(
                (
                    dep
                    for dep in node.depends_on
                    if dep in completed
                    and (not completed[dep].success or completed[dep].is_cancelled)
                ),
                None,
            )
            if failed_dep is not None:
                cancelled_after_failure = True
                result = self._synth_cancelled(
                    node,
                    f"upstream node {failed_dep!r} failed",
                )
                completed[node.id] = result
                yield result
                continue

            # Resolve placeholders against completed results.
            try:
                resolved = resolve_args(node.tool_input, completed)
            except DAGArgResolutionError as exc:
                logger.debug("arg resolution failed for %s: %s", node.id, exc)
                result = self._synth_failure(node, str(exc))
                completed[node.id] = result
                yield result
                continue

            result = await self._dispatch(node, resolved, ctx)
            completed[node.id] = result
            yield result

            if not result.success and not cancelled_after_failure:
                # Note for next iteration: descendants will see a failed
                # dep. We don't need to track separately — the loop above
                # handles it by checking each node's depends_on.
                pass

    # -- Parallel mode -------------------------------------------------------

    async def _run_parallel(
        self,
        dag: TurnDAG,
        ctx: Any,
    ) -> AsyncIterator[DAGNodeResult]:
        pending_ids: set[str] = {node.id for node in dag.nodes}
        completed: dict[str, DAGNodeResult] = {}
        in_flight: dict[str, asyncio.Task[DAGNodeResult]] = {}
        global_sem = asyncio.Semaphore(self._max_concurrency)

        async def _execute_under_limits(
            node: DAGNode,
            resolved: dict[str, Any],
        ) -> DAGNodeResult:
            tool_sem = self._semaphore_for_tool(node.tool_name)
            cap_sem = self._semaphore_for_capability(node.tool_name)
            # Acquire in a deterministic order: global → capability → tool.
            # Avoids deadlock between two nodes that share both a capability
            # and a tool semaphore — not theoretical, just expensive.
            async with global_sem:
                if cap_sem is not None:
                    async with cap_sem:
                        if tool_sem is not None:
                            async with tool_sem:
                                return await self._dispatch(node, resolved, ctx)
                        return await self._dispatch(node, resolved, ctx)
                if tool_sem is not None:
                    async with tool_sem:
                        return await self._dispatch(node, resolved, ctx)
                return await self._dispatch(node, resolved, ctx)

        def _spawn_ready() -> None:
            """Find ready nodes and create tasks for them."""
            for node in dag.ready_nodes(set(completed.keys())):
                if node.id not in pending_ids or node.id in in_flight:
                    continue
                # Resolve args — failure here is a synthesized failure result,
                # not a thrown exception, because we still need the result
                # to flow through the iterator for the SDK contract.
                try:
                    resolved = resolve_args(node.tool_input, completed)
                except DAGArgResolutionError as exc:
                    logger.debug("arg resolution failed for %s: %s", node.id, exc)
                    result = self._synth_failure(node, str(exc))
                    completed[node.id] = result
                    pending_ids.discard(node.id)
                    # Cascade descendants of this failure.
                    self._cascade_failure(
                        node.id,
                        dag,
                        pending_ids,
                        completed,
                        ready_results,
                        f"upstream node {node.id!r} failed",
                    )
                    ready_results.append(result)
                    continue

                task = asyncio.create_task(_execute_under_limits(node, resolved))
                in_flight[node.id] = task

        # Buffer of results that became available without awaiting (cascade,
        # arg-resolve failures). Drained before/after each await.
        ready_results: list[DAGNodeResult] = []

        # Initial spawn.
        _spawn_ready()
        for r in ready_results:
            yield r
        ready_results.clear()

        # Background watcher task that wakes the main loop when cancel fires.
        cancel_watcher: asyncio.Task[None] | None = None

        async def _watch_cancel(event: asyncio.Event) -> None:
            await event.wait()

        if self._cancel_event is not None:
            cancel_watcher = asyncio.create_task(
                _watch_cancel(self._cancel_event),
            )

        try:
            while in_flight or pending_ids:
                # External cancel — bail out and synthesize the rest.
                if self._cancel_event is not None and self._cancel_event.is_set():
                    for task in in_flight.values():
                        task.cancel()
                    # Drain in_flight, capturing whatever completed.
                    for nid, task in list(in_flight.items()):
                        try:
                            result = await task
                            completed[nid] = result
                            pending_ids.discard(nid)
                            yield result
                        except asyncio.CancelledError:
                            logger.debug("task %s cancelled by external signal", nid)
                            cancelled = self._synth_cancelled(
                                dag.get(nid),
                                "external cancel",
                            )
                            completed[nid] = cancelled
                            pending_ids.discard(nid)
                            yield cancelled
                    in_flight.clear()
                    # Synthesize cancelled results for everything still pending.
                    remaining = sorted(
                        pending_ids,
                        key=lambda nid: dag.get(nid).submission_index,
                    )
                    for nid in remaining:
                        cancelled = self._synth_cancelled(
                            dag.get(nid),
                            "external cancel",
                        )
                        completed[nid] = cancelled
                        pending_ids.discard(nid)
                        yield cancelled
                    return

                if not in_flight:
                    # Pending exists but nothing is in flight and nothing is
                    # ready — would loop forever. Either every remaining
                    # node was cancelled (handled above) or we have a bug.
                    if pending_ids:
                        raise DAGExecutionError(
                            f"Scheduler stalled with pending nodes "
                            f"{sorted(pending_ids)!r} and nothing in flight"
                        )
                    break

                # Wake on either a completed task OR cancel signal — important
                # so a long-running task can't block the cancel response.
                wait_set: set[asyncio.Task[Any]] = set(in_flight.values())
                if cancel_watcher is not None and not cancel_watcher.done():
                    wait_set.add(cancel_watcher)
                done_set, _ = await asyncio.wait(
                    wait_set,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Remove the watcher from the completed-task set if it's there
                # so we don't try to treat it as a node task below.
                if cancel_watcher is not None and cancel_watcher in done_set:
                    done_set.discard(cancel_watcher)

                # Map task → node_id for every completed task.
                done_nids: list[str] = []
                for task in done_set:
                    for nid, t in in_flight.items():
                        if t is task:
                            done_nids.append(nid)
                            break

                # Sort by submission_index — more predictable yield order
                # when several tasks land in the same wait() return.
                done_nids.sort(key=lambda nid: dag.get(nid).submission_index)

                for nid in done_nids:
                    task = in_flight.pop(nid)
                    pending_ids.discard(nid)
                    try:
                        result = task.result()
                    except asyncio.CancelledError:
                        logger.debug("task %s cancelled during await", nid)
                        result = self._synth_cancelled(
                            dag.get(nid),
                            "task cancelled",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("task %s raised: %s", nid, exc, exc_info=True)
                        result = self._synth_failure(dag.get(nid), str(exc))
                    completed[nid] = result
                    yield result

                    if not result.success or result.is_cancelled:
                        # Cascade — every descendant gets a synthesized
                        # cancellation. Iterate before spawning new ready
                        # nodes so we don't accidentally spawn a doomed
                        # descendant in the next call.
                        self._cascade_failure(
                            nid,
                            dag,
                            pending_ids,
                            completed,
                            ready_results,
                            f"upstream node {nid!r} failed",
                        )

                # Drain any cascade results synthesized above.
                for r in ready_results:
                    yield r
                ready_results.clear()

                # Spawn anything newly ready.
                _spawn_ready()
                for r in ready_results:
                    yield r
                ready_results.clear()
        finally:
            # Defensive: cancel anything still in flight on a generator close.
            for task in in_flight.values():
                if not task.done():
                    task.cancel()
            if cancel_watcher is not None and not cancel_watcher.done():
                cancel_watcher.cancel()

    # -- Helpers -------------------------------------------------------------

    def _semaphore_for_tool(self, tool_name: str) -> asyncio.Semaphore | None:
        if tool_name not in self._per_tool_concurrency:
            return None
        sem = self._tool_semaphores.get(tool_name)
        if sem is None:
            sem = asyncio.Semaphore(max(1, self._per_tool_concurrency[tool_name]))
            self._tool_semaphores[tool_name] = sem
        return sem

    def _semaphore_for_capability(self, tool_name: str) -> asyncio.Semaphore | None:
        if not self._per_capability_concurrency:
            return None
        spec = self._registry.get(tool_name)
        if spec is None or not spec.capability:
            return None
        cap = spec.capability
        if cap not in self._per_capability_concurrency:
            return None
        sem = self._capability_semaphores.get(cap)
        if sem is None:
            sem = asyncio.Semaphore(max(1, self._per_capability_concurrency[cap]))
            self._capability_semaphores[cap] = sem
        return sem

    def _cascade_failure(
        self,
        failed_id: str,
        dag: TurnDAG,
        pending_ids: set[str],
        completed: dict[str, DAGNodeResult],
        ready_results: list[DAGNodeResult],
        reason: str,
    ) -> None:
        """Synthesize cancelled results for every descendant of *failed_id*."""
        descendants = dag.descendants_of(failed_id)
        # Iterate in topological order so cancellation events stream out
        # in the same order the tasks would have run.
        for node in dag.nodes_in_topological_order():
            if node.id not in descendants:
                continue
            if node.id in completed:
                continue
            cancelled = self._synth_cancelled(node, reason)
            completed[node.id] = cancelled
            pending_ids.discard(node.id)
            ready_results.append(cancelled)

    async def _dispatch(
        self,
        node: DAGNode,
        resolved: dict[str, Any],
        ctx: Any,
    ) -> DAGNodeResult:
        started = time.monotonic()
        try:
            content = await self._executor(node, resolved)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "dispatch failure on %s (%s): %s",
                node.id,
                node.tool_name,
                exc,
                exc_info=True,
            )
            return DAGNodeResult(
                node_id=node.id,
                tool_use_id=node.tool_use_id,
                submission_index=node.submission_index,
                success=False,
                content=[],
                error=str(exc),
                started_at=started,
                completed_at=time.monotonic(),
            )
        # ctx is passed through for forward-compat — unused here today.
        _ = ctx
        return DAGNodeResult(
            node_id=node.id,
            tool_use_id=node.tool_use_id,
            submission_index=node.submission_index,
            success=True,
            content=content,
            started_at=started,
            completed_at=time.monotonic(),
        )

    async def _default_execute(
        self,
        node: DAGNode,
        resolved: dict[str, Any],
    ) -> list[ContentBlock]:
        """Default executor: look up the tool and await its handler.

        Returns the handler output wrapped as a single text-kind
        :class:`ContentBlock`. Stage B2 will replace this with a richer
        executor that mirrors today's ``_execute_single_tool`` semantics
        (allowlist, confirmation, capability tokens, etc.).
        """
        spec = self._registry.get(node.tool_name)
        if spec is None:
            raise DAGExecutionError(f"Unknown tool: {node.tool_name!r}")
        raw_result: Any = spec.handler(**resolved)
        if asyncio.iscoroutine(raw_result):
            raw_result = await raw_result
        if isinstance(raw_result, list):
            items = cast("list[Any]", raw_result)
            if all(isinstance(b, ContentBlock) for b in items):
                return [b for b in items if isinstance(b, ContentBlock)]
        if isinstance(raw_result, str):
            return [ContentBlock(kind="text", text=raw_result)]
        return [ContentBlock(kind="text", text=repr(cast("object", raw_result)))]

    @staticmethod
    def _synth_failure(node: DAGNode, message: str) -> DAGNodeResult:
        now = time.monotonic()
        return DAGNodeResult(
            node_id=node.id,
            tool_use_id=node.tool_use_id,
            submission_index=node.submission_index,
            success=False,
            content=[],
            error=message,
            started_at=now,
            completed_at=now,
        )

    @staticmethod
    def _synth_cancelled(node: DAGNode, reason: str) -> DAGNodeResult:
        now = time.monotonic()
        return DAGNodeResult(
            node_id=node.id,
            tool_use_id=node.tool_use_id,
            submission_index=node.submission_index,
            success=False,
            content=[],
            error=reason,
            is_cancelled=True,
            started_at=now,
            completed_at=now,
        )
