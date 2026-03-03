"""Tests for manifest data models."""

from __future__ import annotations

from obscura.manifest.models import (
    AgentManifest,
    HookDefinition,
    InstructionManifest,
    MCPServerRef,
    PermissionConfig,
    SkillManifest,
    agent_manifest_from_frontmatter,
)


class TestPermissionConfig:
    def test_defaults(self) -> None:
        p = PermissionConfig()
        assert p.allow == []
        assert p.deny == []

    def test_with_values(self) -> None:
        p = PermissionConfig(allow=["Read", "Bash(git *)"], deny=["Bash(rm -rf /)"])
        assert "Read" in p.allow
        assert "Bash(rm -rf /)" in p.deny


class TestHookDefinition:
    def test_command_hook(self) -> None:
        h = HookDefinition(event="preToolUse", bash="echo hi")
        assert h.type == "command"
        assert h.event == "preToolUse"
        assert h.bash == "echo hi"

    def test_python_hook(self) -> None:
        h = HookDefinition(type="python", event="postToolUse", module="my.hook")
        assert h.type == "python"
        assert h.module == "my.hook"


class TestSkillManifest:
    def test_defaults(self) -> None:
        s = SkillManifest(name="search")
        assert s.user_invocable is True
        assert s.allowed_tools == []
        assert s.body == ""

    def test_with_tools(self) -> None:
        s = SkillManifest(name="code", allowed_tools=["Read", "Write"])
        assert s.allowed_tools == ["Read", "Write"]


class TestInstructionManifest:
    def test_defaults(self) -> None:
        i = InstructionManifest()
        assert i.apply_to == []
        assert i.body == ""

    def test_with_globs(self) -> None:
        i = InstructionManifest(apply_to=["**/*.py", "**/*.ts"], body="Use types.")
        assert len(i.apply_to) == 2


class TestMCPServerRef:
    def test_defaults(self) -> None:
        m = MCPServerRef(name="github")
        assert m.transport == "stdio"
        assert m.command == ""

    def test_full_config(self) -> None:
        m = MCPServerRef(
            name="github",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={"GITHUB_TOKEN": "tok"},
        )
        assert m.args == ["-y", "@modelcontextprotocol/server-github"]


class TestAgentManifest:
    def test_defaults(self) -> None:
        a = AgentManifest(name="dev")
        assert a.model == "copilot"
        assert a.system_prompt == ""
        assert a.tools == []
        assert a.mcp_servers == "auto"
        assert a.can_delegate is False
        assert a.agent_type == "loop"
        assert a.max_turns == 25

    def test_full_manifest(self) -> None:
        a = AgentManifest(
            name="researcher",
            model="claude",
            tools=["Read", "Bash"],
            can_delegate=True,
            delegate_allowlist=["code-reviewer"],
            permissions=PermissionConfig(allow=["Read"]),
        )
        assert a.name == "researcher"
        assert a.can_delegate is True
        assert a.permissions.allow == ["Read"]


class TestAgentManifestFromFrontmatter:
    def test_basic(self) -> None:
        metadata = {"name": "dev", "model": "claude", "tools": ["Read"]}
        body = "You are a developer."
        m = agent_manifest_from_frontmatter(metadata, body)
        assert m.name == "dev"
        assert m.model == "claude"
        assert m.tools == ["Read"]
        assert m.system_prompt == "You are a developer."

    def test_hyphenated_keys(self) -> None:
        metadata = {
            "name": "x",
            "mcp-servers": ["github", "memory"],
            "tool-allowlist": ["Read", "Write"],
            "can-delegate": True,
        }
        m = agent_manifest_from_frontmatter(metadata, "")
        assert m.mcp_servers == ["github", "memory"]
        assert m.tool_allowlist == ["Read", "Write"]
        assert m.can_delegate is True

    def test_permissions(self) -> None:
        metadata = {
            "name": "x",
            "permissions": {"allow": ["Read"], "deny": ["Bash(rm *)"]},
        }
        m = agent_manifest_from_frontmatter(metadata, "")
        assert m.permissions.allow == ["Read"]
        assert m.permissions.deny == ["Bash(rm *)"]

    def test_hooks_inline(self) -> None:
        metadata = {
            "name": "x",
            "hooks": [
                {"event": "preToolUse", "bash": "echo test"},
            ],
        }
        m = agent_manifest_from_frontmatter(metadata, "")
        assert len(m.hooks) == 1
        assert m.hooks[0].event == "preToolUse"

    def test_unknown_keys_ignored(self) -> None:
        metadata = {"name": "x", "custom_field": "ignored"}
        m = agent_manifest_from_frontmatter(metadata, "body")
        assert m.name == "x"

    def test_source_path(self) -> None:
        from pathlib import Path
        m = agent_manifest_from_frontmatter(
            {"name": "x"}, "body", source_path=Path("/tmp/x.md")
        )
        assert m.source_path == Path("/tmp/x.md")
