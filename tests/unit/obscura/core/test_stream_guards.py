"""Tests for the per-task tool-call guards in obscura.core.stream_guards.

Covers :func:`check_stream_guards`, :func:`hash_args`, and the
:func:`bind_stream_log` lifecycle. The guards prevent SDK / agent-loop
tool execution from running the same tool with identical args repeatedly
within one user→agent task, and cap the total number of tool calls.

These primitives are imported by every backend (Copilot, Claude, Codex,
OpenAI, ...) and by ``agent_loop_v2`` so behavior is uniform regardless
of which provider drives the loop.
"""

from __future__ import annotations

import json

import pytest

from obscura.core import stream_guards as sg


@pytest.fixture(autouse=True)
def reset_log() -> None:
    """Each test starts with no log bound (fail-open default).

    Used implicitly by pytest via the ``autouse=True`` decorator.
    """
    sg.STREAM_TOOL_LOG.set(None)


def _bind(log: dict[tuple[str, str], int] | None) -> None:
    """Bind a fresh log dict to the ContextVar for the current test."""
    sg.STREAM_TOOL_LOG.set(log)


def test_no_log_bound_fails_open() -> None:
    """Outside a stream lifecycle, guards are inert."""
    assert sg.check_stream_guards("grep", {"pattern": "x"}) is None


def test_first_call_proceeds_and_logs() -> None:
    log: dict[tuple[str, str], int] = {}
    _bind(log)
    assert sg.check_stream_guards("grep", {"pattern": "verify"}) is None
    assert sum(log.values()) == 1


def test_duplicate_identical_call_is_refused() -> None:
    log: dict[tuple[str, str], int] = {}
    _bind(log)
    sg.check_stream_guards("grep", {"pattern": "verify"})
    refusal = sg.check_stream_guards("grep", {"pattern": "verify"})
    assert refusal is not None
    assert refusal["error"] == "duplicate_call_in_same_turn"
    assert refusal["tool"] == "grep"
    assert refusal["prior_call_count"] == 1
    assert "do not re-call" in refusal["message"]


def test_different_args_are_not_duplicates() -> None:
    log: dict[tuple[str, str], int] = {}
    _bind(log)
    assert sg.check_stream_guards("grep", {"pattern": "x"}) is None
    assert sg.check_stream_guards("grep", {"pattern": "y"}) is None
    assert sum(log.values()) == 2


def test_arg_order_does_not_affect_dedup() -> None:
    """Args differing only in dict key order should hash identically."""
    log: dict[tuple[str, str], int] = {}
    _bind(log)
    sg.check_stream_guards("grep", {"a": 1, "b": 2})
    refusal = sg.check_stream_guards("grep", {"b": 2, "a": 1})
    assert refusal is not None
    assert refusal["error"] == "duplicate_call_in_same_turn"


def test_different_tools_with_same_args_are_not_duplicates() -> None:
    log: dict[tuple[str, str], int] = {}
    _bind(log)
    assert sg.check_stream_guards("grep", {"pattern": "x"}) is None
    assert sg.check_stream_guards("rg", {"pattern": "x"}) is None


def test_budget_exhausted_after_max_total_calls() -> None:
    log: dict[tuple[str, str], int] = {}
    _bind(log)
    # Saturate to the limit with distinct calls so dedup doesn't fire first.
    for i in range(sg.MAX_TOTAL_CALLS):
        assert sg.check_stream_guards("grep", {"i": i}) is None
    refusal = sg.check_stream_guards("grep", {"i": "over"})
    assert refusal is not None
    assert refusal["error"] == "tool_budget_exhausted"
    assert refusal["total_calls"] == sg.MAX_TOTAL_CALLS
    assert refusal["limit"] == sg.MAX_TOTAL_CALLS


def test_unhashable_args_do_not_crash() -> None:
    """Non-JSON-serializable args fall back to repr; dedup still works."""
    log: dict[tuple[str, str], int] = {}
    _bind(log)

    class _Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    args = {"obj": _Opaque()}
    assert sg.check_stream_guards("grep", args) is None
    refusal = sg.check_stream_guards("grep", args)
    assert refusal is not None
    assert refusal["error"] == "duplicate_call_in_same_turn"


def test_refusal_payload_serializes_to_json() -> None:
    """refusal_text and direct json.dumps must both succeed."""
    log: dict[tuple[str, str], int] = {}
    _bind(log)
    sg.check_stream_guards("grep", {"pattern": "x"})
    refusal = sg.check_stream_guards("grep", {"pattern": "x"})
    assert refusal is not None
    parsed = json.loads(sg.refusal_text(refusal))
    assert parsed["error"] == "duplicate_call_in_same_turn"


def test_bind_stream_log_isolates_log_per_block() -> None:
    """Two sequential bind blocks see independent logs."""
    with sg.bind_stream_log() as log1:
        sg.check_stream_guards("grep", {"q": "a"})
        assert sum(log1.values()) == 1
    # After exiting, the log is unbound.
    assert sg.STREAM_TOOL_LOG.get() is None
    # A fresh bind starts empty.
    with sg.bind_stream_log() as log2:
        assert log2 != log1
        assert sum(log2.values()) == 0
        sg.check_stream_guards("grep", {"q": "a"})
        assert sum(log2.values()) == 1


def test_bind_stream_log_resets_after_exception() -> None:
    """Exception inside the block must still unbind the log."""
    with pytest.raises(RuntimeError, match="boom"):
        with sg.bind_stream_log():
            sg.check_stream_guards("grep", {"q": "a"})
            raise RuntimeError("boom")
    assert sg.STREAM_TOOL_LOG.get() is None
