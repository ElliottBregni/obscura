"""Tests for obscura.core.dag — TurnDAG, Scheduler, resolve_args."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from obscura.core.dag import (
    DAGArgResolutionError,
    DAGNode,
    DAGNodeResult,
    DAGValidationError,
    Scheduler,
    TurnDAG,
    resolve_args,
)
from obscura.core.tools import ToolRegistry
from obscura.core.types import ContentBlock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(
    nid: str,
    *,
    tool: str = "stub",
    deps: tuple[str, ...] = (),
    idx: int = 0,
    args: dict[str, Any] | None = None,
    tool_use_id: str | None = None,
) -> DAGNode:
    return DAGNode(
        id=nid,
        tool_name=tool,
        tool_input=dict(args or {}),
        depends_on=deps,
        submission_index=idx,
        tool_use_id=tool_use_id or f"tu-{nid}",
    )


def _make_executor(
    *,
    tool_outputs: dict[str, str] | None = None,
    sleeps: dict[str, float] | None = None,
    failures: set[str] | None = None,
    on_call: list[str] | None = None,
):
    """Build a stub executor that records calls and respects per-node sleeps."""
    outs = tool_outputs or {}
    delays = sleeps or {}
    fails = failures or set()
    log = on_call if on_call is not None else []

    async def run(node: DAGNode, resolved: dict[str, Any]) -> list[ContentBlock]:
        log.append(node.id)
        delay = delays.get(node.id, 0.0)
        if delay:
            await asyncio.sleep(delay)
        if node.id in fails:
            raise RuntimeError(f"intentional failure in {node.id}")
        text = outs.get(node.id, f"out-of-{node.id}|{resolved}")
        return [ContentBlock(kind="text", text=text)]

    return run


# ---------------------------------------------------------------------------
# TurnDAG validation
# ---------------------------------------------------------------------------


class TestTurnDAGValidation:
    def test_empty_dag_ok(self):
        dag = TurnDAG(nodes=[])
        assert len(dag) == 0

    def test_duplicate_ids_rejected(self):
        with pytest.raises(DAGValidationError, match="Duplicate"):
            TurnDAG(nodes=[_node("a"), _node("a")])

    def test_unknown_dependency_rejected(self):
        with pytest.raises(DAGValidationError, match="unknown node"):
            TurnDAG(nodes=[_node("a", deps=("ghost",))])

    def test_self_dependency_rejected(self):
        with pytest.raises(DAGValidationError, match="cannot depend on itself"):
            TurnDAG(nodes=[_node("a", deps=("a",))])

    def test_simple_cycle_detected(self):
        with pytest.raises(DAGValidationError, match="Cycle"):
            TurnDAG(
                nodes=[
                    _node("a", deps=("b",)),
                    _node("b", deps=("a",)),
                ],
            )

    def test_three_node_cycle_detected(self):
        with pytest.raises(DAGValidationError, match="Cycle"):
            TurnDAG(
                nodes=[
                    _node("a", deps=("c",)),
                    _node("b", deps=("a",)),
                    _node("c", deps=("b",)),
                ],
            )

    def test_membership_and_get(self):
        dag = TurnDAG(nodes=[_node("a"), _node("b")])
        assert "a" in dag
        assert "missing" not in dag
        assert dag.get("a").id == "a"
        with pytest.raises(DAGValidationError):
            dag.get("missing")


# ---------------------------------------------------------------------------
# Topology and graph queries
# ---------------------------------------------------------------------------


class TestTopologyAndQueries:
    def test_topological_order_respects_dependencies(self):
        # 5 nodes:  a → b, a → c, b → d, c → d, d → e
        dag = TurnDAG(
            nodes=[
                _node("a", idx=0),
                _node("b", deps=("a",), idx=1),
                _node("c", deps=("a",), idx=2),
                _node("d", deps=("b", "c"), idx=3),
                _node("e", deps=("d",), idx=4),
            ],
        )
        order = [n.id for n in dag.nodes_in_topological_order()]
        # a must come before b/c; b/c before d; d before e.
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")
        assert order.index("d") < order.index("e")

    def test_topological_order_stable_by_submission_index(self):
        # No edges, so all are simultaneously ready — submission_index breaks ties.
        dag = TurnDAG(
            nodes=[
                _node("z", idx=2),
                _node("y", idx=1),
                _node("x", idx=0),
            ],
        )
        order = [n.id for n in dag.nodes_in_topological_order()]
        assert order == ["x", "y", "z"]

    def test_ready_nodes_initial_state(self):
        dag = TurnDAG(
            nodes=[
                _node("a", idx=0),
                _node("b", deps=("a",), idx=1),
                _node("c", idx=2),
            ],
        )
        ready = [n.id for n in dag.ready_nodes(set())]
        assert ready == ["a", "c"]  # b waits on a; sorted by submission_index

    def test_ready_nodes_progression(self):
        dag = TurnDAG(
            nodes=[
                _node("a", idx=0),
                _node("b", deps=("a",), idx=1),
                _node("c", deps=("a",), idx=2),
                _node("d", deps=("b", "c"), idx=3),
            ],
        )
        # After a completes:
        ready = [n.id for n in dag.ready_nodes({"a"})]
        assert ready == ["b", "c"]
        # After a, b complete:
        ready = [n.id for n in dag.ready_nodes({"a", "b"})]
        assert ready == ["c"]
        # After a, b, c complete:
        ready = [n.id for n in dag.ready_nodes({"a", "b", "c"})]
        assert ready == ["d"]
        # All complete:
        ready = dag.ready_nodes({"a", "b", "c", "d"})
        assert ready == []

    def test_descendants_of(self):
        # a → b, a → c, b → d, c → d, d → e
        dag = TurnDAG(
            nodes=[
                _node("a"),
                _node("b", deps=("a",)),
                _node("c", deps=("a",)),
                _node("d", deps=("b", "c")),
                _node("e", deps=("d",)),
                _node("isolated"),
            ],
        )
        assert dag.descendants_of("a") == {"b", "c", "d", "e"}
        assert dag.descendants_of("b") == {"d", "e"}
        assert dag.descendants_of("d") == {"e"}
        assert dag.descendants_of("e") == set()
        assert dag.descendants_of("isolated") == set()

    def test_descendants_of_unknown_id_raises(self):
        dag = TurnDAG(nodes=[_node("a")])
        with pytest.raises(DAGValidationError):
            dag.descendants_of("ghost")


# ---------------------------------------------------------------------------
# Argument resolution
# ---------------------------------------------------------------------------


class TestResolveArgs:
    def test_passthrough_when_no_placeholders(self):
        assert resolve_args({"a": 1, "b": "hello"}, {}) == {"a": 1, "b": "hello"}

    def test_text_placeholder(self):
        result = DAGNodeResult(
            node_id="n1",
            tool_use_id="t1",
            submission_index=0,
            success=True,
            content=[ContentBlock(kind="text", text="hello world")],
        )
        out = resolve_args({"msg": "${n1.text}"}, {"n1": result})
        assert out == {"msg": "hello world"}

    def test_text_placeholder_concatenates_multiple_blocks(self):
        result = DAGNodeResult(
            node_id="n1",
            tool_use_id="t1",
            submission_index=0,
            success=True,
            content=[
                ContentBlock(kind="text", text="a"),
                ContentBlock(kind="thinking", text="ignored"),
                ContentBlock(kind="text", text="b"),
            ],
        )
        out = resolve_args({"msg": "${n1.text}"}, {"n1": result})
        assert out == {"msg": "ab"}

    def test_nested_placeholder_in_list_and_dict(self):
        result = DAGNodeResult(
            node_id="n1",
            tool_use_id="t1",
            submission_index=0,
            success=True,
            content=[ContentBlock(kind="text", text="X")],
        )
        out = resolve_args(
            {"items": ["${n1.text}", {"nested": "${n1.text}"}]},
            {"n1": result},
        )
        assert out == {"items": ["X", {"nested": "X"}]}

    def test_missing_node_left_literal(self):
        out = resolve_args({"msg": "${ghost.text}"}, {})
        assert out == {"msg": "${ghost.text}"}

    def test_failed_node_raises(self):
        result = DAGNodeResult(
            node_id="n1",
            tool_use_id="t1",
            submission_index=0,
            success=False,
            content=[],
            error="boom",
        )
        with pytest.raises(DAGArgResolutionError, match="did not succeed"):
            resolve_args({"msg": "${n1.text}"}, {"n1": result})

    def test_cancelled_node_raises(self):
        result = DAGNodeResult(
            node_id="n1",
            tool_use_id="t1",
            submission_index=0,
            success=False,
            content=[],
            is_cancelled=True,
            error="upstream failed",
        )
        with pytest.raises(DAGArgResolutionError):
            resolve_args({"msg": "${n1.text}"}, {"n1": result})

    def test_substring_placeholder_not_substituted(self):
        # Whole-string-only contract — substring matches stay literal.
        result = DAGNodeResult(
            node_id="n1",
            tool_use_id="t1",
            submission_index=0,
            success=True,
            content=[ContentBlock(kind="text", text="X")],
        )
        out = resolve_args({"msg": "prefix ${n1.text} suffix"}, {"n1": result})
        assert out == {"msg": "prefix ${n1.text} suffix"}

    def test_content_block_indexing(self):
        result = DAGNodeResult(
            node_id="n1",
            tool_use_id="t1",
            submission_index=0,
            success=True,
            content=[
                ContentBlock(kind="text", text="first"),
                ContentBlock(kind="text", text="second"),
            ],
        )
        out = resolve_args({"msg": "${n1.content.1.text}"}, {"n1": result})
        assert out == {"msg": "second"}

    def test_unknown_path_raises(self):
        result = DAGNodeResult(
            node_id="n1",
            tool_use_id="t1",
            submission_index=0,
            success=True,
            content=[ContentBlock(kind="text", text="X")],
        )
        with pytest.raises(DAGArgResolutionError, match="not found"):
            resolve_args({"msg": "${n1.does_not_exist}"}, {"n1": result})


# ---------------------------------------------------------------------------
# Scheduler — sequential mode
# ---------------------------------------------------------------------------


class TestSchedulerSequential:
    async def test_single_node_runs(self):
        registry = ToolRegistry()
        log: list[str] = []
        scheduler = Scheduler(
            registry,
            mode="sequential",
            executor=_make_executor(on_call=log),
        )
        dag = TurnDAG(nodes=[_node("a")])
        results = [r async for r in scheduler.run(dag)]
        assert log == ["a"]
        assert len(results) == 1
        assert results[0].node_id == "a"
        assert results[0].success
        assert results[0].content[0].text.startswith("out-of-a")

    async def test_runs_in_submission_order_no_edges(self):
        registry = ToolRegistry()
        log: list[str] = []
        scheduler = Scheduler(
            registry,
            mode="sequential",
            executor=_make_executor(on_call=log),
        )
        dag = TurnDAG(
            nodes=[
                _node("first", idx=0),
                _node("second", idx=1),
                _node("third", idx=2),
            ],
        )
        results = [r async for r in scheduler.run(dag)]
        # Sequential mode is bit-for-bit identical to today: submission order.
        assert log == ["first", "second", "third"]
        assert [r.node_id for r in results] == ["first", "second", "third"]

    async def test_no_concurrency_in_sequential_mode(self):
        registry = ToolRegistry()
        log: list[str] = []
        scheduler = Scheduler(
            registry,
            mode="sequential",
            executor=_make_executor(
                on_call=log,
                sleeps={"a": 0.05, "b": 0.05},
            ),
        )
        dag = TurnDAG(nodes=[_node("a", idx=0), _node("b", idx=1)])
        start = time.monotonic()
        _ = [r async for r in scheduler.run(dag)]
        elapsed = time.monotonic() - start
        # Strict sequential = sum of sleeps. Allow generous timing slop on CI.
        assert elapsed >= 0.09
        assert log == ["a", "b"]

    async def test_failure_cascades_to_descendants(self):
        registry = ToolRegistry()
        log: list[str] = []
        scheduler = Scheduler(
            registry,
            mode="sequential",
            executor=_make_executor(on_call=log, failures={"a"}),
        )
        # a → b → c, isolated d
        dag = TurnDAG(
            nodes=[
                _node("a", idx=0),
                _node("b", deps=("a",), idx=1),
                _node("c", deps=("b",), idx=2),
                _node("d", idx=3),
            ],
        )
        results = {r.node_id: r async for r in scheduler.run(dag)}
        assert log == ["a", "d"]  # b, c never ran (cancelled), d unaffected
        assert results["a"].success is False
        assert results["b"].is_cancelled
        assert results["c"].is_cancelled
        assert results["d"].success

    async def test_arg_resolution_in_sequential(self):
        registry = ToolRegistry()
        log: list[str] = []

        async def exec_(node: DAGNode, resolved: dict[str, Any]) -> list[ContentBlock]:
            log.append(f"{node.id}:{resolved.get('input', '')}")
            return [ContentBlock(kind="text", text=f"result-of-{node.id}")]

        scheduler = Scheduler(
            registry,
            mode="sequential",
            executor=exec_,
        )
        dag = TurnDAG(
            nodes=[
                _node("a", idx=0, args={"input": "raw"}),
                _node(
                    "b",
                    deps=("a",),
                    idx=1,
                    args={"input": "${a.text}"},
                ),
            ],
        )
        results = [r async for r in scheduler.run(dag)]
        assert log == ["a:raw", "b:result-of-a"]
        assert all(r.success for r in results)


# ---------------------------------------------------------------------------
# Scheduler — parallel mode
# ---------------------------------------------------------------------------


class TestSchedulerParallel:
    async def test_independent_siblings_run_concurrently(self):
        registry = ToolRegistry()
        scheduler = Scheduler(
            registry,
            mode="parallel",
            max_concurrency=4,
            executor=_make_executor(sleeps={"a": 0.1, "b": 0.1, "c": 0.1}),
        )
        dag = TurnDAG(
            nodes=[
                _node("a", idx=0),
                _node("b", idx=1),
                _node("c", idx=2),
            ],
        )
        start = time.monotonic()
        results = [r async for r in scheduler.run(dag)]
        elapsed = time.monotonic() - start
        # Three 0.1s sleeps in parallel should take ~0.1s, definitely
        # less than the 0.3s sum.
        assert elapsed < 0.25, f"expected parallel execution, took {elapsed:.3f}s"
        assert {r.node_id for r in results} == {"a", "b", "c"}
        assert all(r.success for r in results)

    async def test_dependencies_serialize(self):
        registry = ToolRegistry()
        log: list[str] = []
        scheduler = Scheduler(
            registry,
            mode="parallel",
            max_concurrency=4,
            executor=_make_executor(
                on_call=log,
                sleeps={"a": 0.05, "b": 0.05, "c": 0.05},
            ),
        )
        # a → b → c — must run strictly in series even in parallel mode.
        dag = TurnDAG(
            nodes=[
                _node("a", idx=0),
                _node("b", deps=("a",), idx=1),
                _node("c", deps=("b",), idx=2),
            ],
        )
        start = time.monotonic()
        _ = [r async for r in scheduler.run(dag)]
        elapsed = time.monotonic() - start
        # Three serial 0.05s sleeps = ~0.15s minimum.
        assert elapsed >= 0.13
        assert log == ["a", "b", "c"]

    async def test_failure_cascades_in_parallel(self):
        registry = ToolRegistry()
        log: list[str] = []
        scheduler = Scheduler(
            registry,
            mode="parallel",
            max_concurrency=4,
            executor=_make_executor(
                on_call=log,
                failures={"a"},
                sleeps={"a": 0.02, "d": 0.05},
            ),
        )
        # a → b → c (cascade), independent d should still run.
        dag = TurnDAG(
            nodes=[
                _node("a", idx=0),
                _node("b", deps=("a",), idx=1),
                _node("c", deps=("b",), idx=2),
                _node("d", idx=3),
            ],
        )
        results = {r.node_id: r async for r in scheduler.run(dag)}
        assert results["a"].success is False
        assert results["b"].is_cancelled
        assert results["c"].is_cancelled
        assert results["d"].success
        assert "a" in log
        assert "d" in log
        # b and c must not have been called.
        assert "b" not in log
        assert "c" not in log

    async def test_external_cancel_synthesizes_results(self):
        registry = ToolRegistry()
        cancel = asyncio.Event()
        log: list[str] = []
        scheduler = Scheduler(
            registry,
            mode="parallel",
            max_concurrency=4,
            cancel_event=cancel,
            executor=_make_executor(
                on_call=log,
                sleeps={"a": 0.5, "b": 0.5, "c": 0.5},
            ),
        )
        dag = TurnDAG(
            nodes=[
                _node("a", idx=0),
                _node("b", idx=1),
                _node("c", idx=2),
            ],
        )

        async def fire_cancel():
            await asyncio.sleep(0.05)
            cancel.set()

        canceller = asyncio.create_task(fire_cancel())
        results = {r.node_id: r async for r in scheduler.run(dag)}
        await canceller
        # Every node must have a result (SDK contract): success=False, is_cancelled=True.
        assert set(results.keys()) == {"a", "b", "c"}
        for r in results.values():
            assert r.is_cancelled or not r.success

    async def test_per_tool_concurrency_serializes_same_tool(self):
        registry = ToolRegistry()
        log: list[tuple[str, float]] = []

        async def exec_(node: DAGNode, resolved: dict[str, Any]) -> list[ContentBlock]:
            log.append((node.id, time.monotonic()))
            await asyncio.sleep(0.05)
            return [ContentBlock(kind="text", text="ok")]

        scheduler = Scheduler(
            registry,
            mode="parallel",
            max_concurrency=4,
            per_tool_concurrency={"slow": 1},
            executor=exec_,
        )
        # Two siblings on the same tool — should serialize despite parallel mode.
        dag = TurnDAG(
            nodes=[
                _node("a", tool="slow", idx=0),
                _node("b", tool="slow", idx=1),
            ],
        )
        start = time.monotonic()
        _ = [r async for r in scheduler.run(dag)]
        elapsed = time.monotonic() - start
        # Per-tool semaphore = 1 forces serial. Two 0.05s = ~0.1s minimum.
        assert elapsed >= 0.09, f"expected serialization, took {elapsed:.3f}s"

    async def test_max_concurrency_bounded(self):
        registry = ToolRegistry()
        in_flight = 0
        max_seen = [0]

        async def exec_(node: DAGNode, resolved: dict[str, Any]) -> list[ContentBlock]:
            nonlocal in_flight
            in_flight += 1
            max_seen[0] = max(max_seen[0], in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1
            return [ContentBlock(kind="text", text="ok")]

        scheduler = Scheduler(
            registry,
            mode="parallel",
            max_concurrency=2,
            executor=exec_,
        )
        # 6 independent nodes; cap = 2.
        dag = TurnDAG(nodes=[_node(f"n{i}", idx=i) for i in range(6)])
        _ = [r async for r in scheduler.run(dag)]
        assert max_seen[0] <= 2
        assert max_seen[0] >= 1

    async def test_arg_resolution_failure_cascades(self):
        registry = ToolRegistry()
        log: list[str] = []
        scheduler = Scheduler(
            registry,
            mode="parallel",
            executor=_make_executor(on_call=log, failures={"a"}),
        )
        # a fails → b's "${a.text}" fails to resolve → c (depends on b) also fails.
        dag = TurnDAG(
            nodes=[
                _node("a", idx=0),
                _node("b", deps=("a",), idx=1, args={"x": "${a.text}"}),
                _node("c", deps=("b",), idx=2),
            ],
        )
        results = {r.node_id: r async for r in scheduler.run(dag)}
        assert results["a"].success is False
        assert results["b"].is_cancelled
        assert results["c"].is_cancelled
        # Only "a" should have been dispatched; b and c cascade-cancelled.
        assert log == ["a"]

    async def test_yields_in_completion_order(self):
        registry = ToolRegistry()
        # Make c finish first, then b, then a — yield order should reflect.
        scheduler = Scheduler(
            registry,
            mode="parallel",
            max_concurrency=4,
            executor=_make_executor(
                sleeps={"a": 0.15, "b": 0.10, "c": 0.05},
            ),
        )
        dag = TurnDAG(
            nodes=[
                _node("a", idx=0),
                _node("b", idx=1),
                _node("c", idx=2),
            ],
        )
        results = [r async for r in scheduler.run(dag)]
        assert [r.node_id for r in results] == ["c", "b", "a"]
