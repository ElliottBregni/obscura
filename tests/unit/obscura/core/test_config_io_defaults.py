"""Tests for config_io default-merging helpers.

Covers ``apply_agent_defaults``, ``_deep_merge_new``, and ``load_merged_agents``.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from obscura.core.config_io import (
    _deep_merge_new,
    apply_agent_defaults,
    load_merged_agents,
)


# ---------------------------------------------------------------------------
# _deep_merge_new
# ---------------------------------------------------------------------------


class TestDeepMergeNew:
    def test_basic_scalar_override(self) -> None:
        base = {"a": 1, "b": 2}
        override = {"b": 99}
        result = _deep_merge_new(base, override)
        assert result == {"a": 1, "b": 99}

    def test_nested_dict_merge(self) -> None:
        base = {"outer": {"x": 1, "y": 2}}
        override = {"outer": {"y": 10, "z": 30}}
        result = _deep_merge_new(base, override)
        assert result == {"outer": {"x": 1, "y": 10, "z": 30}}

    def test_list_replacement_not_merge(self) -> None:
        base = {"items": [1, 2, 3]}
        override = {"items": [4, 5]}
        result = _deep_merge_new(base, override)
        assert result == {"items": [4, 5]}

    def test_no_mutation_of_base(self) -> None:
        base = {"nested": {"a": 1}}
        base_snapshot = copy.deepcopy(base)
        override = {"nested": {"b": 2}}
        _deep_merge_new(base, override)
        assert base == base_snapshot

    def test_no_mutation_of_override(self) -> None:
        base = {"a": 1}
        override = {"nested": {"b": 2}}
        override_snapshot = copy.deepcopy(override)
        _deep_merge_new(base, override)
        assert override == override_snapshot

    def test_empty_base(self) -> None:
        result = _deep_merge_new({}, {"key": "val"})
        assert result == {"key": "val"}

    def test_empty_override(self) -> None:
        result = _deep_merge_new({"key": "val"}, {})
        assert result == {"key": "val"}

    def test_deeply_nested_merge(self) -> None:
        base = {"a": {"b": {"c": 1, "d": 2}}}
        override = {"a": {"b": {"d": 99, "e": 100}}}
        result = _deep_merge_new(base, override)
        assert result == {"a": {"b": {"c": 1, "d": 99, "e": 100}}}


# ---------------------------------------------------------------------------
# apply_agent_defaults
# ---------------------------------------------------------------------------


class TestApplyAgentDefaults:
    def test_merges_defaults_into_agents(self) -> None:
        raw: dict[str, Any] = {
            "defaults": {"model": "gpt-4", "temperature": 0.7},
            "agents": [
                {"name": "alice"},
                {"name": "bob"},
            ],
        }
        result = apply_agent_defaults(raw)
        for agent in result["agents"]:
            assert agent["model"] == "gpt-4"
            assert agent["temperature"] == 0.7

    def test_agent_values_override_defaults(self) -> None:
        raw: dict[str, Any] = {
            "defaults": {"model": "gpt-4", "temperature": 0.7},
            "agents": [
                {"name": "alice", "model": "claude-3"},
            ],
        }
        result = apply_agent_defaults(raw)
        assert result["agents"][0]["model"] == "claude-3"
        assert result["agents"][0]["temperature"] == 0.7

    def test_deep_merges_nested_dicts(self) -> None:
        raw: dict[str, Any] = {
            "defaults": {
                "capabilities": {"grant": ["read", "write"]},
            },
            "agents": [
                {
                    "name": "alice",
                    "capabilities": {"deny": ["delete"]},
                },
            ],
        }
        result = apply_agent_defaults(raw)
        agent = result["agents"][0]
        assert agent["capabilities"]["grant"] == ["read", "write"]
        assert agent["capabilities"]["deny"] == ["delete"]

    def test_no_mutation_of_input(self) -> None:
        raw: dict[str, Any] = {
            "defaults": {"model": "gpt-4"},
            "agents": [{"name": "alice"}],
        }
        raw_snapshot = copy.deepcopy(raw)
        apply_agent_defaults(raw)
        assert raw == raw_snapshot

    def test_strips_defaults_key_from_output(self) -> None:
        raw: dict[str, Any] = {
            "defaults": {"model": "gpt-4"},
            "agents": [{"name": "alice"}],
        }
        result = apply_agent_defaults(raw)
        assert "defaults" not in result

    def test_noop_when_no_defaults_key(self) -> None:
        raw: dict[str, Any] = {
            "agents": [{"name": "alice"}],
        }
        result = apply_agent_defaults(raw)
        assert result is raw  # same object returned, no copy needed

    def test_handles_empty_agents_list(self) -> None:
        raw: dict[str, Any] = {
            "defaults": {"model": "gpt-4"},
            "agents": [],
        }
        result = apply_agent_defaults(raw)
        assert result["agents"] == []
        assert "defaults" not in result

    def test_handles_agents_as_list_of_dicts(self) -> None:
        raw: dict[str, Any] = {
            "defaults": {"provider": "openai"},
            "agents": [
                {"name": "a1", "mode": "code"},
                {"name": "a2", "mode": "chat"},
            ],
        }
        result = apply_agent_defaults(raw)
        assert len(result["agents"]) == 2
        for agent in result["agents"]:
            assert agent["provider"] == "openai"
        assert result["agents"][0]["mode"] == "code"
        assert result["agents"][1]["mode"] == "chat"

    def test_handles_agents_as_dict_of_dicts(self) -> None:
        raw: dict[str, Any] = {
            "defaults": {"provider": "openai"},
            "agents": {
                "a1": {"mode": "code"},
                "a2": {"mode": "chat"},
            },
        }
        result = apply_agent_defaults(raw)
        assert result["agents"]["a1"]["provider"] == "openai"
        assert result["agents"]["a2"]["provider"] == "openai"
        assert result["agents"]["a1"]["mode"] == "code"

    def test_non_dict_defaults_is_noop(self) -> None:
        raw: dict[str, Any] = {
            "defaults": "not-a-dict",
            "agents": [{"name": "alice"}],
        }
        result = apply_agent_defaults(raw)
        # Returns the original unchanged because defaults is not a dict.
        assert result is raw

    def test_preserves_non_agent_keys(self) -> None:
        raw: dict[str, Any] = {
            "defaults": {"model": "gpt-4"},
            "agents": [{"name": "alice"}],
            "version": "1.0",
            "metadata": {"author": "test"},
        }
        result = apply_agent_defaults(raw)
        assert result["version"] == "1.0"
        assert result["metadata"] == {"author": "test"}
        assert "defaults" not in result


# ---------------------------------------------------------------------------
# load_merged_agents — file-based tests
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Helper to write a YAML file."""
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


class TestLoadMergedAgents:
    def test_primary_agents_override_catalog_by_name(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "agents-available.yaml",
            {
                "agents": [
                    {"name": "bot", "model": "catalog-model", "temperature": 0.5},
                ],
            },
        )
        _write_yaml(
            tmp_path / "agents.yaml",
            {
                "agents": [
                    {"name": "bot", "model": "primary-model"},
                ],
            },
        )
        result = load_merged_agents(tmp_path)
        assert "bot" in result
        assert result["bot"]["model"] == "primary-model"

    def test_catalog_agents_default_enabled_false(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "agents-available.yaml",
            {
                "agents": [
                    {"name": "catalog-only"},
                ],
            },
        )
        # No primary file.
        result = load_merged_agents(tmp_path, include_disabled=True)
        assert "catalog-only" in result
        assert result["catalog-only"].get("enabled") is False

    def test_primary_agents_default_enabled_true(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "agents.yaml",
            {
                "agents": [
                    {"name": "primary-agent"},
                ],
            },
        )
        result = load_merged_agents(tmp_path)
        assert "primary-agent" in result
        assert result["primary-agent"].get("enabled") is True

    def test_missing_catalog_file_graceful(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "agents.yaml",
            {
                "agents": [
                    {"name": "solo"},
                ],
            },
        )
        # No agents-available.yaml — should not raise.
        result = load_merged_agents(tmp_path)
        assert "solo" in result

    def test_missing_primary_file_graceful(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "agents-available.yaml",
            {
                "agents": [
                    {"name": "catalog-bot"},
                ],
            },
        )
        # No agents.yaml — should not raise.  Catalog agents are disabled by
        # default so with include_disabled=False we get nothing.
        result = load_merged_agents(tmp_path)
        assert result == {}

    def test_include_disabled_returns_all(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "agents-available.yaml",
            {
                "agents": [
                    {"name": "disabled-bot"},
                ],
            },
        )
        _write_yaml(
            tmp_path / "agents.yaml",
            {
                "agents": [
                    {"name": "enabled-bot"},
                ],
            },
        )
        result = load_merged_agents(tmp_path, include_disabled=True)
        assert "disabled-bot" in result
        assert "enabled-bot" in result

    def test_disabled_catalog_agents_filtered_by_default(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "agents-available.yaml",
            {
                "agents": [
                    {"name": "hidden-bot"},
                ],
            },
        )
        _write_yaml(
            tmp_path / "agents.yaml",
            {
                "agents": [
                    {"name": "visible-bot"},
                ],
            },
        )
        result = load_merged_agents(tmp_path)
        assert "hidden-bot" not in result
        assert "visible-bot" in result

    def test_applies_defaults_from_each_file_independently(
        self, tmp_path: Path
    ) -> None:
        _write_yaml(
            tmp_path / "agents-available.yaml",
            {
                "defaults": {"provider": "catalog-provider"},
                "agents": [
                    {"name": "cat-agent"},
                ],
            },
        )
        _write_yaml(
            tmp_path / "agents.yaml",
            {
                "defaults": {"provider": "primary-provider"},
                "agents": [
                    {"name": "pri-agent"},
                ],
            },
        )
        result = load_merged_agents(tmp_path, include_disabled=True)
        assert result["cat-agent"]["provider"] == "catalog-provider"
        assert result["pri-agent"]["provider"] == "primary-provider"

    def test_both_files_missing_returns_empty(self, tmp_path: Path) -> None:
        result = load_merged_agents(tmp_path)
        assert result == {}

    def test_dict_format_agents(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "agents.yaml",
            {
                "agents": {
                    "my-agent": {"model": "gpt-4"},
                },
            },
        )
        result = load_merged_agents(tmp_path)
        assert "my-agent" in result
        assert result["my-agent"]["model"] == "gpt-4"
        # Name should be injected from the dict key.
        assert result["my-agent"]["name"] == "my-agent"
