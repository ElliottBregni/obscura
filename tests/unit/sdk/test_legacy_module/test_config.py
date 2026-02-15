"""Tests for VaultSync config parsing (agents/INDEX.md, repos/INDEX.md)."""

from __future__ import annotations

from pathlib import Path

from scripts.sync import VaultSync


class TestConfigParsing:
    """Config parsing: agent registry, repo list, agent-target mapping."""

    def test_agent_path_mapping(self, sync_instance: VaultSync) -> None:
        assert sync_instance.get_agent_target("copilot") == ".github"
        assert sync_instance.get_agent_target("claude") == ".claude"
        assert sync_instance.get_agent_target("cursor") == ".cursor"
        assert sync_instance.get_agent_target("custom") == ".custom"

    def test_registered_agents(self, sync_instance: VaultSync) -> None:
        agents = sync_instance.get_registered_agents()
        assert "copilot" in agents, f"copilot not in {agents}"
        assert "claude" in agents, f"claude not in {agents}"

    def test_managed_repos(
        self, sync_instance: VaultSync, mock_repo: Path
    ) -> None:
        repos = sync_instance.get_managed_repos()
        names = [r.name for r in repos]
        assert mock_repo.name in names, f"{mock_repo.name} not in {names}"
        assert isinstance(repos[0], Path), "get_managed_repos should return list[Path]"
