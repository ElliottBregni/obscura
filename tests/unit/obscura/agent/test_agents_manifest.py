"""Tests for manifest-driven AgentConfig and SupervisorConfig."""

from __future__ import annotations

from pathlib import Path

from obscura.agent.agents import AgentConfig
from obscura.agent.supervisor import SupervisorConfig
from obscura.manifest.models import (
    AgentManifest,
    MCPServerRef,
    SkillManifest,
)


class TestAgentConfigFromManifest:
    def test_basic_manifest(self) -> None:
        manifest = AgentManifest(
            name="dev",
            model="claude",
            system_prompt="You are a developer.",
            max_turns=10,
        )
        config = AgentConfig.from_manifest(manifest)
        assert config.name == "dev"
        assert config.model == "claude"
        assert "You are a developer." in config.system_prompt
        assert config.max_iterations == 10

    def test_tools_and_tags(self) -> None:
        manifest = AgentManifest(
            name="x",
            tools=["Read", "Write"],
            tags=["test"],
        )
        config = AgentConfig.from_manifest(manifest)
        assert config.tools == ["Read", "Write"]
        assert config.tags == ["test"]

    def test_delegation(self) -> None:
        manifest = AgentManifest(
            name="lead",
            can_delegate=True,
            delegate_allowlist=["code-reviewer"],
            max_delegation_depth=5,
        )
        config = AgentConfig.from_manifest(manifest)
        assert config.can_delegate is True
        assert config.delegate_allowlist == ["code-reviewer"]
        assert config.max_delegation_depth == 5

    def test_tool_allowlist(self) -> None:
        manifest = AgentManifest(
            name="restricted",
            tool_allowlist=["Read", "Bash"],
        )
        config = AgentConfig.from_manifest(manifest)
        assert config.tool_allowlist == ["Read", "Bash"]

    def test_tool_allowlist_none(self) -> None:
        manifest = AgentManifest(name="open")
        config = AgentConfig.from_manifest(manifest)
        assert config.tool_allowlist is None

    def test_mcp_from_refs(self) -> None:
        manifest = AgentManifest(
            name="x",
            mcp_server_refs=[
                MCPServerRef(
                    name="github",
                    command="npx",
                    args=["-y", "gh-server"],
                    env={"TOKEN": "tok"},
                ),
            ],
        )
        config = AgentConfig.from_manifest(manifest)
        assert config.mcp.enabled is True
        assert len(config.mcp.servers) == 1
        assert config.mcp.servers[0]["command"] == "npx"

    def test_mcp_server_names(self) -> None:
        manifest = AgentManifest(
            name="x",
            mcp_servers=["github", "memory"],
        )
        config = AgentConfig.from_manifest(manifest)
        assert config.mcp.server_names == ["github", "memory"]

    def test_system_prompt_includes_skills(self) -> None:
        manifest = AgentManifest(
            name="dev",
            system_prompt="You are a developer.",
            skills=[SkillManifest(name="search", body="Search the web.")],
        )
        config = AgentConfig.from_manifest(manifest)
        assert "You are a developer." in config.system_prompt
        assert "## Skill: search" in config.system_prompt
        assert "Search the web." in config.system_prompt


class TestSupervisorConfigFromDirectory:
    def test_scans_agent_md_files(self, tmp_path: Path) -> None:
        (tmp_path / "dev.agent.md").write_text(
            "---\nname: dev\nmodel: claude\nmax-turns: 15\n---\nYou are a dev.",
            encoding="utf-8",
        )
        (tmp_path / "reviewer.agent.md").write_text(
            "---\nname: reviewer\nmodel: copilot\n---\nYou review code.",
            encoding="utf-8",
        )
        config = SupervisorConfig.from_directory(tmp_path)
        assert len(config.agents) == 2
        names = {a.name for a in config.agents}
        assert "dev" in names
        assert "reviewer" in names

    def test_agent_fields_mapped(self, tmp_path: Path) -> None:
        (tmp_path / "lead.agent.md").write_text(
            "---\nname: lead\nmodel: claude\nagent-type: loop\nmax-turns: 30\ncan-delegate: true\n---\nYou lead.",
            encoding="utf-8",
        )
        config = SupervisorConfig.from_directory(tmp_path)
        assert len(config.agents) == 1
        agent = config.agents[0]
        assert agent.name == "lead"
        assert agent.model == "claude"
        assert agent.type == "loop"
        assert agent.max_turns == 30
        assert agent.can_delegate is True

    def test_empty_directory(self, tmp_path: Path) -> None:
        config = SupervisorConfig.from_directory(tmp_path)
        assert config.agents == []

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        config = SupervisorConfig.from_directory(tmp_path / "nope")
        assert config.agents == []

    def test_tool_allowlist_mapped(self, tmp_path: Path) -> None:
        (tmp_path / "safe.agent.md").write_text(
            "---\nname: safe\ntool-allowlist:\n  - Read\n  - Grep\n---\nSafe agent.",
            encoding="utf-8",
        )
        config = SupervisorConfig.from_directory(tmp_path)
        assert len(config.agents) == 1
        assert config.agents[0].tool_allowlist == ["Read", "Grep"]
