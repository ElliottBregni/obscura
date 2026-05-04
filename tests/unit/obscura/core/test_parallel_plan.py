"""Tests for obscura.core.parallel_plan — parsing, DAG conversion, ToolSpec."""

from __future__ import annotations

import json
from typing import Any

import pytest

from obscura.core.dag import DAGNode, TurnDAG
from obscura.core.parallel_plan import (
    PARALLEL_PLAN_TOOL_NAME,
    ParallelPlanInput,
    ParallelPlanNode,
    ParallelPlanValidationError,
    build_turn_dag_from_parallel_plan,
    make_parallel_plan_tool_spec,
    parse_parallel_plan_input,
)
from obscura.core.types import ToolSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node_dict(
    id: str,
    tool: str = "stub",
    args: dict[str, Any] | None = None,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    """Build a raw plan-node dict for use in parse_parallel_plan_input."""
    out: dict[str, Any] = {"id": id, "tool": tool, "args": args or {}}
    if depends_on is not None:
        out["depends_on"] = depends_on
    return out


def _plan(*nodes: dict[str, Any]) -> dict[str, Any]:
    """Build a raw plan dict from one or more node dicts."""
    return {"nodes": list(nodes)}


# ---------------------------------------------------------------------------
# parse_parallel_plan_input — happy path
# ---------------------------------------------------------------------------


class TestParseValidPlans:
    def test_single_node(self):
        raw = _plan(_node_dict("a", tool="echo", args={"text": "hi"}))
        plan = parse_parallel_plan_input(raw)
        assert isinstance(plan, ParallelPlanInput)
        assert len(plan.nodes) == 1
        node = plan.nodes[0]
        assert isinstance(node, ParallelPlanNode)
        assert node.id == "a"
        assert node.tool == "echo"
        assert node.args == {"text": "hi"}
        assert node.depends_on == ()

    def test_multiple_independent_nodes(self):
        raw = _plan(
            _node_dict("a", tool="t1"),
            _node_dict("b", tool="t2"),
            _node_dict("c", tool="t3"),
        )
        plan = parse_parallel_plan_input(raw)
        assert len(plan.nodes) == 3
        assert tuple(n.id for n in plan.nodes) == ("a", "b", "c")
        assert all(n.depends_on == () for n in plan.nodes)

    def test_chain_dependencies(self):
        raw = _plan(
            _node_dict("a", tool="t1"),
            _node_dict("b", tool="t2", depends_on=["a"]),
            _node_dict("c", tool="t3", depends_on=["b"]),
        )
        plan = parse_parallel_plan_input(raw)
        assert plan.nodes[0].depends_on == ()
        assert plan.nodes[1].depends_on == ("a",)
        assert plan.nodes[2].depends_on == ("b",)

    def test_diamond_shape(self):
        # a -> {b, c} -> d
        raw = _plan(
            _node_dict("a", tool="t1"),
            _node_dict("b", tool="t2", depends_on=["a"]),
            _node_dict("c", tool="t3", depends_on=["a"]),
            _node_dict("d", tool="t4", depends_on=["b", "c"]),
        )
        plan = parse_parallel_plan_input(raw)
        assert plan.nodes[3].depends_on == ("b", "c")

    def test_empty_depends_on_explicit(self):
        # Explicitly empty list is the same as omitted.
        raw = _plan(_node_dict("a", tool="t1", depends_on=[]))
        plan = parse_parallel_plan_input(raw)
        assert plan.nodes[0].depends_on == ()

    def test_args_with_placeholders_passes_through(self):
        # parse() does not validate placeholders — the runtime resolves
        # them at dispatch time. Bad refs surface there with useful
        # context.
        raw = _plan(
            _node_dict("a", tool="t1"),
            _node_dict(
                "b",
                tool="t2",
                args={"input": "${a.text}", "extra": 42},
                depends_on=["a"],
            ),
        )
        plan = parse_parallel_plan_input(raw)
        assert plan.nodes[1].args == {"input": "${a.text}", "extra": 42}

    def test_returns_immutable_tuple(self):
        raw = _plan(_node_dict("a"), _node_dict("b"))
        plan = parse_parallel_plan_input(raw)
        assert isinstance(plan.nodes, tuple)


# ---------------------------------------------------------------------------
# parse_parallel_plan_input — rejection cases
# ---------------------------------------------------------------------------


class TestParseRejections:
    def test_root_not_a_dict(self):
        with pytest.raises(ParallelPlanValidationError, match="object"):
            parse_parallel_plan_input("not a dict")

    def test_missing_nodes_field(self):
        with pytest.raises(ParallelPlanValidationError, match="nodes"):
            parse_parallel_plan_input({"unrelated": True})

    def test_nodes_not_a_list(self):
        with pytest.raises(ParallelPlanValidationError, match="must be an array"):
            parse_parallel_plan_input({"nodes": "should be a list"})

    def test_nodes_is_dict_rejected(self):
        with pytest.raises(ParallelPlanValidationError, match="must be an array"):
            parse_parallel_plan_input({"nodes": {"a": "foo"}})

    def test_empty_nodes_list_rejected(self):
        with pytest.raises(ParallelPlanValidationError, match="must not be empty"):
            parse_parallel_plan_input({"nodes": []})

    def test_node_not_an_object(self):
        with pytest.raises(ParallelPlanValidationError, match=r"nodes\[0\]"):
            parse_parallel_plan_input({"nodes": ["string node"]})

    def test_missing_id_field(self):
        with pytest.raises(
            ParallelPlanValidationError, match="missing required field 'id'"
        ):
            parse_parallel_plan_input({"nodes": [{"tool": "t1", "args": {}}]})

    def test_missing_tool_field(self):
        with pytest.raises(
            ParallelPlanValidationError, match="missing required field 'tool'"
        ):
            parse_parallel_plan_input({"nodes": [{"id": "a", "args": {}}]})

    def test_missing_args_field(self):
        with pytest.raises(
            ParallelPlanValidationError, match="missing required field 'args'"
        ):
            parse_parallel_plan_input({"nodes": [{"id": "a", "tool": "t1"}]})

    def test_non_string_id(self):
        with pytest.raises(ParallelPlanValidationError, match="must be a string"):
            parse_parallel_plan_input({"nodes": [{"id": 42, "tool": "t1", "args": {}}]})

    def test_empty_id(self):
        with pytest.raises(ParallelPlanValidationError, match="non-empty"):
            parse_parallel_plan_input({"nodes": [{"id": "", "tool": "t1", "args": {}}]})

    def test_non_string_tool(self):
        with pytest.raises(ParallelPlanValidationError, match="must be a string"):
            parse_parallel_plan_input({"nodes": [{"id": "a", "tool": 99, "args": {}}]})

    def test_args_not_an_object(self):
        with pytest.raises(
            ParallelPlanValidationError, match="'args' must be an object"
        ):
            parse_parallel_plan_input(
                {"nodes": [{"id": "a", "tool": "t1", "args": "no"}]}
            )

    def test_depends_on_not_a_list(self):
        with pytest.raises(ParallelPlanValidationError, match="depends_on"):
            parse_parallel_plan_input(
                {"nodes": [{"id": "a", "tool": "t1", "args": {}, "depends_on": "a"}]}
            )

    def test_depends_on_contains_non_string(self):
        with pytest.raises(ParallelPlanValidationError, match=r"depends_on\[0\]"):
            parse_parallel_plan_input(
                {"nodes": [{"id": "a", "tool": "t1", "args": {}, "depends_on": [123]}]}
            )

    def test_duplicate_ids_rejected(self):
        raw = _plan(_node_dict("a"), _node_dict("a"))
        with pytest.raises(ParallelPlanValidationError, match="duplicate"):
            parse_parallel_plan_input(raw)

    def test_unknown_dependency_rejected(self):
        raw = _plan(
            _node_dict("a", depends_on=["ghost"]),
        )
        with pytest.raises(ParallelPlanValidationError, match="unknown"):
            parse_parallel_plan_input(raw)

    def test_self_dependency_rejected(self):
        raw = _plan(
            _node_dict("a", depends_on=["a"]),
        )
        with pytest.raises(ParallelPlanValidationError):
            parse_parallel_plan_input(raw)

    def test_simple_cycle_rejected(self):
        raw = _plan(
            _node_dict("a", depends_on=["b"]),
            _node_dict("b", depends_on=["a"]),
        )
        with pytest.raises(ParallelPlanValidationError, match=r"[Cc]ycle"):
            parse_parallel_plan_input(raw)

    def test_three_node_cycle_rejected(self):
        raw = _plan(
            _node_dict("a", depends_on=["c"]),
            _node_dict("b", depends_on=["a"]),
            _node_dict("c", depends_on=["b"]),
        )
        with pytest.raises(ParallelPlanValidationError, match=r"[Cc]ycle"):
            parse_parallel_plan_input(raw)


# ---------------------------------------------------------------------------
# build_turn_dag_from_parallel_plan
# ---------------------------------------------------------------------------


class TestBuildTurnDAG:
    def test_returns_turn_dag(self):
        plan = parse_parallel_plan_input(_plan(_node_dict("a", tool="t1")))
        dag = build_turn_dag_from_parallel_plan(plan)
        assert isinstance(dag, TurnDAG)
        assert len(dag) == 1

    def test_node_shape_matches_input(self):
        plan = parse_parallel_plan_input(
            _plan(
                _node_dict("a", tool="echo", args={"text": "hi"}),
                _node_dict(
                    "b",
                    tool="reverse",
                    args={"input": "${a.text}"},
                    depends_on=["a"],
                ),
            )
        )
        dag = build_turn_dag_from_parallel_plan(plan)

        node_a = dag.get("a")
        assert isinstance(node_a, DAGNode)
        assert node_a.tool_name == "echo"
        assert node_a.tool_input == {"text": "hi"}
        assert node_a.depends_on == ()
        assert node_a.submission_index == 0

        node_b = dag.get("b")
        assert node_b.tool_name == "reverse"
        assert node_b.tool_input == {"input": "${a.text}"}
        assert node_b.depends_on == ("a",)
        assert node_b.submission_index == 1

    def test_topological_order_preserves_input_order(self):
        plan = parse_parallel_plan_input(
            _plan(
                _node_dict("a", tool="t1"),
                _node_dict("b", tool="t2", depends_on=["a"]),
                _node_dict("c", tool="t3", depends_on=["b"]),
            )
        )
        dag = build_turn_dag_from_parallel_plan(plan)
        topo = [n.id for n in dag.nodes_in_topological_order()]
        assert topo == ["a", "b", "c"]

    def test_diamond_dag_constructible(self):
        plan = parse_parallel_plan_input(
            _plan(
                _node_dict("a", tool="t1"),
                _node_dict("b", tool="t2", depends_on=["a"]),
                _node_dict("c", tool="t3", depends_on=["a"]),
                _node_dict("d", tool="t4", depends_on=["b", "c"]),
            )
        )
        dag = build_turn_dag_from_parallel_plan(plan)
        # Descendants of 'a' must include all of b/c/d.
        assert dag.descendants_of("a") == {"b", "c", "d"}

    def test_submission_index_offset_zero_default(self):
        plan = parse_parallel_plan_input(_plan(_node_dict("a"), _node_dict("b")))
        dag = build_turn_dag_from_parallel_plan(plan)
        assert dag.get("a").submission_index == 0
        assert dag.get("b").submission_index == 1

    def test_submission_index_offset_applied(self):
        plan = parse_parallel_plan_input(
            _plan(_node_dict("a"), _node_dict("b"), _node_dict("c"))
        )
        dag = build_turn_dag_from_parallel_plan(plan, submission_index_offset=10)
        assert dag.get("a").submission_index == 10
        assert dag.get("b").submission_index == 11
        assert dag.get("c").submission_index == 12

    def test_tool_use_id_blank_by_default(self):
        # The plan is itself a single SDK tool_use; per-node ids are
        # unused in this scaffold.
        plan = parse_parallel_plan_input(_plan(_node_dict("a")))
        dag = build_turn_dag_from_parallel_plan(plan)
        assert dag.get("a").tool_use_id == ""

    def test_args_dict_isolated_from_input(self):
        # Mutating the resulting DAG node's tool_input must not affect
        # the source plan, and vice versa.
        plan = parse_parallel_plan_input(_plan(_node_dict("a", args={"x": 1})))
        dag = build_turn_dag_from_parallel_plan(plan)
        node = dag.get("a")
        # Source plan args is frozen via the dataclass, but the args
        # dict itself is mutable. The DAG node's tool_input should be a
        # distinct dict.
        assert node.tool_input == {"x": 1}
        assert node.tool_input is not plan.nodes[0].args


# ---------------------------------------------------------------------------
# make_parallel_plan_tool_spec
# ---------------------------------------------------------------------------


class TestMakeParallelPlanToolSpec:
    def test_returns_tool_spec(self):
        spec = make_parallel_plan_tool_spec()
        assert isinstance(spec, ToolSpec)

    def test_name_matches_canonical(self):
        spec = make_parallel_plan_tool_spec()
        assert spec.name == PARALLEL_PLAN_TOOL_NAME
        assert spec.name == "parallel_plan"

    def test_description_mentions_dag_and_dependencies(self):
        spec = make_parallel_plan_tool_spec()
        # Description should explain when to use the tool. We check for
        # signal words rather than exact text so wording can evolve
        # without brittle test failures.
        desc_lower = spec.description.lower()
        assert "depends_on" in desc_lower
        assert "parallel" in desc_lower
        # Should warn against trivial single-tool use.
        assert "single" in desc_lower or "regular" in desc_lower

    def test_description_documents_handler_is_noop(self):
        # Stage C scaffolding contract: callers should know that the
        # handler is a stub and the agent loop is expected to intercept.
        spec = make_parallel_plan_tool_spec()
        desc_lower = spec.description.lower()
        assert "intercept" in desc_lower or "no-op" in desc_lower

    def test_input_schema_shape(self):
        spec = make_parallel_plan_tool_spec()
        params = spec.parameters
        assert params["type"] == "object"
        assert "nodes" in params["required"]

        nodes_schema = params["properties"]["nodes"]
        assert nodes_schema["type"] == "array"
        assert nodes_schema.get("minItems") == 1

        item = nodes_schema["items"]
        assert item["type"] == "object"
        assert set(item["required"]) == {"id", "tool", "args"}
        assert item["properties"]["id"]["type"] == "string"
        assert item["properties"]["tool"]["type"] == "string"
        assert item["properties"]["args"]["type"] == "object"

        deps = item["properties"]["depends_on"]
        assert deps["type"] == "array"
        assert deps["items"]["type"] == "string"

    def test_handler_returns_json_echo(self):
        spec = make_parallel_plan_tool_spec()
        nodes = [{"id": "a", "tool": "t1", "args": {"x": 1}}]
        result = spec.handler(nodes=nodes)
        # Handler is a no-op stub. It MUST return a JSON string so the
        # agent loop's existing string→ContentBlock path keeps working
        # if the interception logic is bypassed.
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert "parallel_plan_echo" in parsed
        assert parsed["parallel_plan_echo"]["nodes"] == nodes

    def test_capability_set(self):
        # Capability gating lets operators disable the tool without
        # changing source.
        spec = make_parallel_plan_tool_spec()
        assert spec.capability == "system.parallel_plan"

    def test_side_effects_marked_none(self):
        # The tool itself doesn't do anything — the agent loop expands
        # it into individual tool calls, which carry their own side
        # effects metadata. Marking the wrapper as 'none' lets it run
        # in concurrency-safe groups when intercepted.
        spec = make_parallel_plan_tool_spec()
        assert spec.side_effects == "none"
