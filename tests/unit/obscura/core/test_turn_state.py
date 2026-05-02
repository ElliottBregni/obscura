"""TurnState immutability + replace() semantics.

The agent loop relies on TurnState being a frozen dataclass that
produces a fresh copy on every ``.replace()`` — mutating the original
in place would corrupt the loop's per-iteration snapshot.
"""

from __future__ import annotations

from obscura.core.agent_loop import TurnState
from obscura.core.types import ToolCallInfo


def test_replace_returns_new_instance_with_changed_field() -> None:
    state = TurnState(turn=1, accumulated_text="hello")
    new = state.replace(turn=2)

    assert new is not state
    assert new.turn == 2
    assert state.turn == 1


def test_replace_preserves_untouched_fields() -> None:
    state = TurnState(
        turn=3,
        accumulated_text="prior",
        accumulated_chars=42,
        finish_reason="stop",
    )
    new = state.replace(turn=4)

    assert new.accumulated_text == "prior"
    assert new.accumulated_chars == 42
    assert new.finish_reason == "stop"


def test_replace_handles_multiple_fields() -> None:
    state = TurnState(turn=0)
    new = state.replace(turn=5, turn_text="foo", accumulated_text="bar")

    assert new.turn == 5
    assert new.turn_text == "foo"
    assert new.accumulated_text == "bar"


def test_replace_handles_collection_fields() -> None:
    """tuple/frozenset fields must round-trip cleanly through replace()."""
    tc = ToolCallInfo(tool_use_id="x", name="t", input={})
    state = TurnState()
    new = state.replace(
        tool_calls=(tc,),
        emitted_keys=frozenset({"a", "b"}),
    )

    assert new.tool_calls == (tc,)
    assert new.emitted_keys == frozenset({"a", "b"})
    # Original untouched
    assert state.tool_calls == ()
    assert state.emitted_keys == frozenset()


def test_add_tool_call_appends_to_tuple() -> None:
    tc1 = ToolCallInfo(tool_use_id="1", name="a", input={})
    tc2 = ToolCallInfo(tool_use_id="2", name="b", input={})
    state = TurnState().add_tool_call(tc1).add_tool_call(tc2)

    assert state.tool_calls == (tc1, tc2)


def test_add_emitted_key_unions_into_frozenset() -> None:
    state = TurnState().add_emitted_key("x").add_emitted_key("y")
    assert state.emitted_keys == frozenset({"x", "y"})

    # Adding a duplicate is a no-op
    again = state.add_emitted_key("x")
    assert again.emitted_keys == frozenset({"x", "y"})
