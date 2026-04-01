"""Tests for obscura.agent.definitions — agent definition loading."""

from __future__ import annotations

from pathlib import Path

from obscura.agent.definitions import (
    AgentDefinition,
    definition_to_config_dict,
    load_agent_definition,
    load_definitions_dir,
    resolve_all_definitions,
)


def test_load_toml_frontmatter(tmp_path: Path) -> None:
    md = tmp_path / "test-agent.md"
    md.write_text(
        '+++\nname = "test"\ndescription = "A test agent"\n'
        'tools = ["Read", "Grep"]\nmodel = "inherit"\nmax_turns = 30\n'
        "+++\n\nYou are a test agent.\n",
        encoding="utf-8",
    )
    defn = load_agent_definition(md, source="local")
    assert defn.name == "test"
    assert defn.description == "A test agent"
    assert defn.tools == ("Read", "Grep")
    assert defn.model == "inherit"
    assert defn.max_turns == 30
    assert "test agent" in defn.system_prompt


def test_load_definitions_dir(tmp_path: Path) -> None:
    (tmp_path / "agent-a.md").write_text(
        '+++\nname = "alpha"\n+++\nAlpha prompt.\n', encoding="utf-8"
    )
    (tmp_path / "agent-b.md").write_text(
        '+++\nname = "beta"\n+++\nBeta prompt.\n', encoding="utf-8"
    )
    defs = load_definitions_dir(tmp_path, source="test")
    assert "alpha" in defs
    assert "beta" in defs


def test_definition_to_config_dict_inherit() -> None:
    defn = AgentDefinition(
        name="test",
        model="inherit",
        system_prompt="Hello",
        tools=("Read",),
        max_turns=20,
    )
    cfg = definition_to_config_dict(defn, parent_model="claude")
    assert cfg["provider"] == "claude"
    assert cfg["tool_allowlist"] == ["Read"]
    assert cfg["max_iterations"] == 20


def test_definition_to_config_dict_specific_model() -> None:
    defn = AgentDefinition(name="test", model="gpt-4o", tools=())
    cfg = definition_to_config_dict(defn, parent_model="claude")
    assert cfg["provider"] == "gpt-4o"
    assert cfg["tool_allowlist"] is None  # empty tuple → None


def test_resolve_all_includes_builtins() -> None:
    defs = resolve_all_definitions()
    assert "general-purpose" in defs
    assert "explore" in defs
    assert "plan" in defs
    assert "verification" in defs
    assert defs["explore"].source == "built-in"
