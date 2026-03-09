"""Tests for obscura.core.compiler.validator — Compiled output validation."""

from __future__ import annotations

from obscura.core.compiler.compiled import (
    CompiledAgent,
    CompiledPolicy,
    CompiledWorkspace,
)
from obscura.core.compiler.validator import validate_workspace


def _agent(
    name: str = "a",
    *,
    mode: str = "task",
    agent_type: str = "loop",
    plugins: tuple[str, ...] = (),
    max_iterations: int = 10,
    tool_allowlist: frozenset[str] | None = None,
    tool_denylist: frozenset[str] = frozenset(),
) -> CompiledAgent:
    return CompiledAgent(
        name=name,
        template_name="t",
        mode=mode,
        agent_type=agent_type,
        provider="copilot",
        plugins=plugins,
        max_iterations=max_iterations,
        tool_allowlist=tool_allowlist,
        tool_denylist=tool_denylist,
    )


def _workspace(
    name: str = "ws",
    *,
    agents: tuple[CompiledAgent, ...] = (),
    startup_agents: tuple[str, ...] = (),
) -> CompiledWorkspace:
    return CompiledWorkspace(
        name=name,
        agents=agents,
        startup_agents=startup_agents,
    )


class TestValidateWorkspace:
    def test_valid_workspace(self) -> None:
        ws = _workspace(
            agents=(_agent("dev"), _agent("reviewer")),
            startup_agents=("dev",),
        )
        errors = validate_workspace(ws)
        assert errors == []

    def test_empty_workspace_valid(self) -> None:
        ws = _workspace()
        errors = validate_workspace(ws)
        assert errors == []

    def test_missing_startup_agent(self) -> None:
        ws = _workspace(
            agents=(_agent("dev"),),
            startup_agents=("missing",),
        )
        errors = validate_workspace(ws)
        assert len(errors) == 1
        assert "missing" in str(errors[0])

    def test_invalid_mode(self) -> None:
        ws = _workspace(agents=(_agent("a", mode="invalid"),))
        errors = validate_workspace(ws)
        assert any("invalid mode" in str(e) for e in errors)

    def test_invalid_agent_type(self) -> None:
        ws = _workspace(agents=(_agent("a", agent_type="invalid"),))
        errors = validate_workspace(ws)
        assert any("invalid agent_type" in str(e) for e in errors)

    def test_invalid_max_iterations(self) -> None:
        ws = _workspace(agents=(_agent("a", max_iterations=0),))
        errors = validate_workspace(ws)
        assert any("max_iterations" in str(e) for e in errors)

    def test_missing_plugin(self) -> None:
        ws = _workspace(agents=(_agent("a", plugins=("git", "missing")),))
        errors = validate_workspace(ws, available_plugins=frozenset(["git"]))
        assert len(errors) == 1
        assert "missing" in str(errors[0])

    def test_plugin_check_skipped_when_none(self) -> None:
        ws = _workspace(agents=(_agent("a", plugins=("anything",)),))
        errors = validate_workspace(ws, available_plugins=None)
        assert errors == []

    def test_tool_allowlist_denylist_overlap(self) -> None:
        ws = _workspace(
            agents=(
                _agent(
                    "a",
                    tool_allowlist=frozenset(["bash", "read_file"]),
                    tool_denylist=frozenset(["bash"]),
                ),
            ),
        )
        errors = validate_workspace(ws)
        assert any("allowlist and denylist" in str(e) for e in errors)

    def test_duplicate_agent_names(self) -> None:
        ws = _workspace(agents=(_agent("dev"), _agent("dev")))
        errors = validate_workspace(ws)
        assert any("Duplicate" in str(e) for e in errors)

    def test_multiple_errors(self) -> None:
        ws = _workspace(
            agents=(
                _agent("a", mode="bad"),
                _agent("a", agent_type="bad"),
            ),
            startup_agents=("missing",),
        )
        errors = validate_workspace(ws)
        assert len(errors) >= 3
