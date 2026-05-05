"""obscura.core.parallel_plan — system tool that emits a structured DAG.

Stage C scaffolding for the agent-loop refactor. This module defines a
``parallel_plan`` system tool the model can call to declare a batch of
tool calls with explicit dependencies (a DAG). The agent loop will
intercept calls to ``parallel_plan`` *before* dispatch, parse the plan,
build a :class:`~obscura.core.dag.TurnDAG`, and execute it via the
:class:`~obscura.core.dag.Scheduler`.

This file is **scaffolding only** — it lands as code but the agent loop
does not yet advertise the tool. Stage D will turn it on after eval.

Why this is a tool and not a model-level feature: the model's existing
"emit several ``tool_use`` blocks in a row" capability already runs them
in parallel via today's intra-batch parallel groups, but it cannot
express *dependencies*. A model that needs B's output as input to C
today has to round-trip through the agent loop. ``parallel_plan`` lets
the model declare ``C depends_on B``, and the runtime threads B's
result through ``${B.text}`` placeholders without an extra turn.

Layout::

    ParallelPlanNode      — one node in a model-emitted plan
    ParallelPlanInput     — root container, validated tuple of nodes
    parse_parallel_plan_input(raw)
                          — validate raw dict from the model and build
                            a ParallelPlanInput
    build_turn_dag_from_parallel_plan(plan, submission_index_offset)
                          — convert to a TurnDAG suitable for Scheduler
    make_parallel_plan_tool_spec()
                          — ToolSpec factory for agent-loop registration

The tool's handler is a no-op stub that echoes the parsed input back as
JSON. The agent loop is expected to intercept the tool call and run the
Scheduler itself; the handler exists only so the tool is registerable
through the normal :class:`~obscura.core.tools.ToolRegistry` pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from obscura.core.dag import (
    DAGNode,
    DAGValidationError,
    TurnDAG,
)
from obscura.core.types import ToolSpec

__all__ = [
    "PARALLEL_PLAN_TOOL_NAME",
    "ParallelPlanInput",
    "ParallelPlanNode",
    "ParallelPlanValidationError",
    "build_turn_dag_from_parallel_plan",
    "make_parallel_plan_tool_spec",
    "parse_parallel_plan_input",
]


# Canonical tool name. The agent loop checks for this string when
# deciding whether to intercept a tool_use block.
PARALLEL_PLAN_TOOL_NAME = "parallel_plan"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ParallelPlanValidationError(ValueError):
    """Raised when a raw plan dict fails validation.

    Distinct from :class:`~obscura.core.dag.DAGValidationError` so callers
    can tell a malformed plan envelope apart from a malformed DAG (e.g. a
    plan that references unknown ids vs. one whose ``nodes`` field is not
    a list).
    """


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

# ParallelPlanNode.args is the heterogeneous JSON arg shape of arbitrary
# tools — the plan layer is generic across every registered ToolSpec, so
# args is legitimately ``dict[str, Any]`` here. Likewise the parser walks
# untrusted model JSON and narrows from ``object`` -> ``dict[str, Any]``
# at the seam.


def _empty_str_any_dict() -> dict[str, Any]:
    return {}


@dataclass(frozen=True)
class ParallelPlanNode:
    """One node in a model-emitted parallel plan.

    Attributes
    ----------
    id
        Unique identifier within this plan. Used as the key in
        ``depends_on`` edges and ``${id.path}`` placeholders.
    tool
        The name of the tool to dispatch.
    args
        Arguments for the tool. May contain ``${node_id.path}`` strings
        that the runtime resolves against completed upstream results.
    depends_on
        Ids of upstream nodes this node waits on. Empty tuple means the
        node is ready to run immediately.
    """

    id: str
    tool: str
    args: dict[str, Any] = field(default_factory=_empty_str_any_dict)
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParallelPlanInput:
    """Root container for a parallel plan.

    Carries an immutable tuple of :class:`ParallelPlanNode` values. Built
    by :func:`parse_parallel_plan_input` from the raw dict the model
    emits as the ``parallel_plan`` tool's input.
    """

    nodes: tuple[ParallelPlanNode, ...]


# ---------------------------------------------------------------------------
# Parsing & validation
# ---------------------------------------------------------------------------


def _require_str(value: object, field_name: str, where: str) -> str:
    if not isinstance(value, str):
        raise ParallelPlanValidationError(
            f"{where}: field {field_name!r} must be a string, "
            f"got {type(value).__name__}"
        )
    if not value:
        raise ParallelPlanValidationError(
            f"{where}: field {field_name!r} must be a non-empty string"
        )
    return value


def _parse_node(raw: object, index: int) -> ParallelPlanNode:
    where = f"nodes[{index}]"
    if not isinstance(raw, dict):
        raise ParallelPlanValidationError(
            f"{where}: must be an object, got {type(raw).__name__}"
        )
    raw_node: dict[str, Any] = raw  # type: ignore[assignment]

    # Required fields.
    if "id" not in raw_node:
        raise ParallelPlanValidationError(f"{where}: missing required field 'id'")
    if "tool" not in raw_node:
        raise ParallelPlanValidationError(f"{where}: missing required field 'tool'")
    if "args" not in raw_node:
        raise ParallelPlanValidationError(f"{where}: missing required field 'args'")

    node_id = _require_str(raw_node["id"], "id", where)
    tool_name = _require_str(raw_node["tool"], "tool", where)

    args_raw = raw_node["args"]
    if not isinstance(args_raw, dict):
        raise ParallelPlanValidationError(
            f"{where}: field 'args' must be an object, got {type(args_raw).__name__}"
        )
    args: dict[str, Any] = dict(args_raw)  # type: ignore[arg-type]

    deps_raw = raw_node.get("depends_on", [])
    if not isinstance(deps_raw, list):
        raise ParallelPlanValidationError(
            f"{where}: field 'depends_on' must be an array, "
            f"got {type(deps_raw).__name__}"
        )
    deps_list: list[Any] = deps_raw  # type: ignore[assignment]
    deps: list[str] = []
    for dep_index, dep in enumerate(deps_list):
        if not isinstance(dep, str):
            raise ParallelPlanValidationError(
                f"{where}: depends_on[{dep_index}] must be a string, "
                f"got {type(dep).__name__}"
            )
        if not dep:
            raise ParallelPlanValidationError(
                f"{where}: depends_on[{dep_index}] must be non-empty"
            )
        deps.append(dep)

    return ParallelPlanNode(
        id=node_id,
        tool=tool_name,
        args=args,
        depends_on=tuple(deps),
    )


def parse_parallel_plan_input(raw: object) -> ParallelPlanInput:
    """Validate a raw plan input and return a :class:`ParallelPlanInput`.

    Accepts ``object`` rather than ``dict[str, Any]`` because the input
    is untrusted model output — the validator must defend against any
    JSON-shaped value, not just dicts.

    Performs strict validation. Specifically rejects:

    * ``raw`` not a dict, or missing the ``nodes`` key.
    * ``nodes`` not a list.
    * ``nodes`` empty.
    * Any node missing ``id``/``tool``/``args``, or with non-string
      ``id``/``tool``.
    * Duplicate node ids.
    * ``depends_on`` referencing an unknown id.
    * Cycles (delegated to :class:`~obscura.core.dag.TurnDAG`).

    On any failure raises :class:`ParallelPlanValidationError` with a
    message that names the offending field/index.
    """
    if not isinstance(raw, dict):
        raise ParallelPlanValidationError(
            f"plan must be an object, got {type(raw).__name__}"
        )
    raw_dict: dict[str, Any] = raw  # type: ignore[assignment]

    if "nodes" not in raw_dict:
        raise ParallelPlanValidationError("plan: missing required field 'nodes'")

    nodes_raw = raw_dict["nodes"]
    if not isinstance(nodes_raw, list):
        raise ParallelPlanValidationError(
            f"plan: field 'nodes' must be an array, got {type(nodes_raw).__name__}"
        )
    nodes_list: list[Any] = nodes_raw  # type: ignore[assignment]
    if not nodes_list:
        raise ParallelPlanValidationError("plan: field 'nodes' must not be empty")

    parsed_nodes: list[ParallelPlanNode] = [
        _parse_node(node_raw, index) for index, node_raw in enumerate(nodes_list)
    ]

    # Check duplicates explicitly so the error message mentions
    # "duplicate" rather than the indirect TurnDAG error.
    seen_ids: set[str] = set()
    for node in parsed_nodes:
        if node.id in seen_ids:
            raise ParallelPlanValidationError(f"plan: duplicate node id {node.id!r}")
        seen_ids.add(node.id)

    # Validate edges + acyclicity by constructing a TurnDAG. We translate
    # DAGValidationError -> ParallelPlanValidationError so callers only
    # need to catch one error type at this layer.
    plan = ParallelPlanInput(nodes=tuple(parsed_nodes))
    try:
        # Discard the result — we just want the validation side effect.
        # build_turn_dag_from_parallel_plan re-runs the same validation
        # later when callers actually want to schedule, but that's fine:
        # validation is cheap, and we want parse() to be a complete
        # check so callers can rely on a successful parse meaning a
        # schedulable plan.
        _ = build_turn_dag_from_parallel_plan(plan)
    except DAGValidationError as exc:
        raise ParallelPlanValidationError(f"plan: {exc}") from exc

    return plan


# ---------------------------------------------------------------------------
# DAG construction
# ---------------------------------------------------------------------------


def build_turn_dag_from_parallel_plan(
    plan: ParallelPlanInput,
    submission_index_offset: int = 0,
) -> TurnDAG:
    """Convert a :class:`ParallelPlanInput` to a :class:`TurnDAG`.

    Each :class:`ParallelPlanNode` becomes a :class:`DAGNode`. The
    ``submission_index`` of a DAG node is its zero-based position in the
    plan's ``nodes`` tuple, plus *submission_index_offset*. Stage B2 /
    Stage D integration uses the offset to merge plan nodes with
    surrounding non-plan tool calls in a single turn while keeping a
    consistent global ordering.

    Raises :class:`~obscura.core.dag.DAGValidationError` if the plan is
    structurally invalid (cycle, unknown dep, duplicate id). Most callers
    should go through :func:`parse_parallel_plan_input` first, which
    catches those at parse time.

    Notes
    -----
    The resulting :class:`DAGNode` does not carry a real ``tool_use_id``
    — the plan is itself a *single* SDK ``tool_use`` block, and the
    individual node calls are an implementation detail of the runtime.
    Callers building tool_result envelopes for the next turn should
    aggregate node results into a single content block keyed off the
    parent ``parallel_plan`` tool_use_id, not per node.
    """
    dag_nodes: list[DAGNode] = [
        DAGNode(
            id=plan_node.id,
            tool_name=plan_node.tool,
            tool_input=dict(plan_node.args),
            depends_on=plan_node.depends_on,
            submission_index=submission_index_offset + index,
            tool_use_id="",
        )
        for index, plan_node in enumerate(plan.nodes)
    ]
    return TurnDAG(nodes=dag_nodes)


# ---------------------------------------------------------------------------
# ToolSpec factory
# ---------------------------------------------------------------------------


_TOOL_DESCRIPTION = (
    "Emit a structured DAG of tool calls. Use this when you have multiple "
    "tool calls that can run in parallel and/or have explicit dependencies "
    "between them.\n\n"
    "How it works:\n"
    "- Provide a list of `nodes`. Each node has an `id`, a `tool` name, "
    "and an `args` object for that tool.\n"
    "- Use `depends_on` to declare ids of upstream nodes that must complete "
    "before this node starts. Independent siblings run concurrently.\n"
    "- Use `${node_id.text}` placeholders inside `args` to thread an upstream "
    "node's text result into a downstream node. Other paths supported: "
    "`${node_id.error}`, `${node_id.content.0.text}`.\n"
    "- The runtime executes the DAG and returns a single tool_result block "
    "containing per-node results. You will not see intermediate calls.\n\n"
    "When NOT to use this:\n"
    "- A single tool call. Just emit a regular `tool_use` block instead.\n"
    "- A trivial sequence with no real parallelism. Regular tool calls are "
    "simpler and equally fast.\n\n"
    "Note: this tool's handler is a no-op stub. The agent loop intercepts "
    "the call and runs the DAG via its scheduler before dispatch ever "
    "reaches the handler. The handler echo'ing back its input is a "
    "diagnostic fallback, not the production path."
)


_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "minItems": 1,
            "description": (
                "Ordered list of nodes in the plan. Order is informational; "
                "actual execution order is determined by `depends_on` edges."
            ),
            "items": {
                "type": "object",
                "required": ["id", "tool", "args"],
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Unique node identifier within this plan.",
                    },
                    "tool": {
                        "type": "string",
                        "description": "Tool name to dispatch.",
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Tool arguments. May use ${node_id.text} placeholders "
                            "to reference upstream node results."
                        ),
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                        "description": (
                            "Ids of upstream nodes that must complete before "
                            "this node starts."
                        ),
                    },
                },
            },
        },
    },
    "required": ["nodes"],
}


def _parallel_plan_handler(**kwargs: Any) -> str:
    """No-op handler that echoes the input as JSON.

    The agent loop intercepts ``parallel_plan`` calls and runs the DAG
    itself; this handler is only invoked if the interception logic is
    bypassed (e.g. during direct unit tests). Returning a JSON echo of
    the input keeps test assertions simple and surfaces the fact that
    interception did not happen, which would be a bug.
    """
    return json.dumps({"parallel_plan_echo": kwargs}, sort_keys=True)


def make_parallel_plan_tool_spec() -> ToolSpec:
    """Return the :class:`ToolSpec` for the ``parallel_plan`` system tool.

    Stage D registration: backends that opt in call this factory and
    register the result with their tool host. Until Stage D, no backend
    advertises the tool to the model.
    """
    return ToolSpec(
        name=PARALLEL_PLAN_TOOL_NAME,
        description=_TOOL_DESCRIPTION,
        parameters=_TOOL_INPUT_SCHEMA,
        handler=_parallel_plan_handler,
        side_effects="none",
        capability="system.parallel_plan",
    )
