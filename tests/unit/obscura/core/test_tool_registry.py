"""Unit tests for obscura.core.tools.ToolRegistry."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import pytest

from obscura.core.tools import ToolRegistry
from obscura.core.types import SideEffects, ToolSpec

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"desc for {name}",
        parameters={"type": "object", "properties": {}},
        handler=lambda: name,  # type: ignore[arg-type, return-value]
        side_effects=SideEffects.NONE,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_register_and_get_exact() -> None:
    reg = ToolRegistry()
    reg.register(_spec("read_text_file"))
    assert reg.get("read_text_file") is not None


def test_get_unknown_name_returns_none() -> None:
    reg = ToolRegistry()
    assert reg.get("absolutely_no_such_tool_xyz") is None


def test_mcp_prefixed_lookup() -> None:
    reg = ToolRegistry()
    reg.register(_spec("run_shell"))
    assert reg.get("mcp__obscura__run_shell") is not None


def test_case_insensitive_lookup() -> None:
    reg = ToolRegistry()
    reg.register(_spec("run_shell"))
    result = reg.get("Run_Shell")
    assert result is not None
    assert result.name == "run_shell"


def test_disable_marks_tool_as_disabled() -> None:
    """disable() marks the tool; get() still finds it, is_disabled() returns True."""
    reg = ToolRegistry()
    reg.register(_spec("tool_a"))
    reg.disable("tool_a")
    assert reg.is_disabled("tool_a")
    # get() does NOT filter disabled — callers use is_disabled() to gate calls
    assert reg.get("tool_a") is not None


def test_disable_hides_from_all() -> None:
    reg = ToolRegistry()
    reg.register(_spec("tool_b"))
    reg.disable("tool_b")
    assert all(s.name != "tool_b" for s in reg.all())


def test_enable_restores_tool() -> None:
    reg = ToolRegistry()
    reg.register(_spec("tool_c"))
    reg.disable("tool_c")
    reg.enable("tool_c")
    assert reg.get("tool_c") is not None


def test_all_returns_registered_tools() -> None:
    reg = ToolRegistry()
    reg.register(_spec("t1"))
    reg.register(_spec("t2"))
    names = {s.name for s in reg.all()}
    assert {"t1", "t2"} <= names


def test_is_disabled_reflects_state() -> None:
    reg = ToolRegistry()
    reg.register(_spec("togglable"))
    assert not reg.is_disabled("togglable")
    reg.disable("togglable")
    assert reg.is_disabled("togglable")
    reg.enable("togglable")
    assert not reg.is_disabled("togglable")


def test_alias_bash_resolves_to_run_shell() -> None:
    """Built-in alias map: 'bash' → 'run_shell'."""
    reg = ToolRegistry()
    reg.register(_spec("run_shell"))
    result = reg.get("bash")
    assert result is not None
    assert result.name == "run_shell"


def test_alias_clock_resolves_to_current_time() -> None:
    reg = ToolRegistry()
    reg.register(_spec("current_time"))
    result = reg.get("clock")
    assert result is not None
    assert result.name == "current_time"


def test_skill_alias_removed_to_prevent_typeerror() -> None:
    """Regression guard: 'skill' must not alias to 'task'.

    The Claude SDK Skill tool's argument schema {skill, args} doesn't
    line up with task(prompt, target, timeout_seconds) — routing it via
    a name alias crashed task() with
    TypeError("unexpected keyword argument 'skill'"), which the renderer
    silently displayed with a ✓ glyph and confused the agent into
    looping. If you're reintroducing this alias, build a proper
    parameter-remapping bridge first.
    """
    reg = ToolRegistry()
    reg.register(_spec("task"))
    # Either resolves to None (no such tool) OR resolves to something
    # other than 'task' — both are acceptable outcomes. What's NOT
    # acceptable is silently routing to task() with mismatched kwargs.
    result = reg.get("skill")
    if result is not None:
        assert result.name != "task", (
            "alias 'skill' → 'task' was reintroduced; see "
            "obscura/core/tools.py comment about Skill SDK schema "
            "mismatch."
        )


def test_agent_alias_removed_to_prevent_typeerror() -> None:
    """Regression guard: 'agent' must not alias to 'task'.

    Same root cause as the 'skill' alias: the Claude SDK Agent tool
    takes {description, prompt, subagent_type} — those kwargs don't
    match task()'s signature. Routing via a name alias produced a
    swallowed TypeError. See test above.
    """
    reg = ToolRegistry()
    reg.register(_spec("task"))
    result = reg.get("agent")
    if result is not None:
        assert result.name != "task", (
            "alias 'agent' → 'task' was reintroduced; see "
            "obscura/core/tools.py comment about Agent SDK schema "
            "mismatch."
        )
