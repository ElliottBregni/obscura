"""Tests for manifest loader."""

from __future__ import annotations

import json
from pathlib import Path

from obscura.manifest.loader import ManifestLoader


class TestLoadAgentManifest:
    def test_basic_agent_md(self, tmp_path: Path) -> None:
        f = tmp_path / "dev.agent.md"
        f.write_text(
            "---\nname: dev\nmodel: claude\ntools:\n  - Read\n  - Bash\n---\n"
            "You are a developer agent.",
            encoding="utf-8",
        )
        loader = ManifestLoader(base_dir=tmp_path)
        m = loader.load_agent_manifest(f)
        assert m.name == "dev"
        assert m.model == "claude"
        assert m.tools == ["Read", "Bash"]
        assert m.system_prompt == "You are a developer agent."

    def test_mcp_servers_from_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "x.agent.md"
        f.write_text(
            "---\nname: x\nmcp-servers:\n  - github\n  - memory\n---\nPrompt.",
            encoding="utf-8",
        )
        loader = ManifestLoader(base_dir=tmp_path)
        m = loader.load_agent_manifest(f)
        assert m.mcp_servers == ["github", "memory"]

    def test_source_path_set(self, tmp_path: Path) -> None:
        f = tmp_path / "a.agent.md"
        f.write_text("---\nname: a\n---\nBody.", encoding="utf-8")
        m = ManifestLoader(base_dir=tmp_path).load_agent_manifest(f)
        assert m.source_path == f


class TestLoadAgentManifests:
    def test_scans_directory(self, tmp_path: Path) -> None:
        (tmp_path / "dev.agent.md").write_text(
            "---\nname: dev\n---\nDev prompt.", encoding="utf-8"
        )
        (tmp_path / "research.agent.md").write_text(
            "---\nname: research\n---\nResearch prompt.", encoding="utf-8"
        )
        (tmp_path / "not-agent.md").write_text("ignored", encoding="utf-8")
        loader = ManifestLoader(base_dir=tmp_path)
        manifests = loader.load_agent_manifests(tmp_path)
        assert len(manifests) == 2
        names = {m.name for m in manifests}
        assert names == {"dev", "research"}

    def test_empty_directory(self, tmp_path: Path) -> None:
        assert ManifestLoader(base_dir=tmp_path).load_agent_manifests(tmp_path) == []

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        assert ManifestLoader().load_agent_manifests(tmp_path / "missing") == []


class TestLoadSkillManifest:
    def test_with_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "SKILL.md"
        f.write_text(
            "---\nname: search\ndescription: Web search\n"
            "user-invocable: true\nallowed-tools:\n  - WebSearch\n---\n"
            "Search the web for information.",
            encoding="utf-8",
        )
        skill = ManifestLoader(base_dir=tmp_path).load_skill_manifest(f)
        assert skill.name == "search"
        assert skill.description == "Web search"
        assert skill.user_invocable is True
        assert skill.allowed_tools == ["WebSearch"]
        assert "Search the web" in skill.body

    def test_without_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "code-review.md"
        f.write_text("Review code for bugs.", encoding="utf-8")
        skill = ManifestLoader(base_dir=tmp_path).load_skill_manifest(f)
        assert skill.name == "code-review"
        assert skill.body == "Review code for bugs."

    def test_skills_from_directory(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "a.md").write_text(
            "---\nname: alpha\n---\nAlpha.", encoding="utf-8"
        )
        (skills_dir / "b.md").write_text(
            "---\nname: beta\n---\nBeta.", encoding="utf-8"
        )
        skills = ManifestLoader(base_dir=tmp_path).load_skills_from_directory(skills_dir)
        assert len(skills) == 2


class TestLoadInstructionManifest:
    def test_with_apply_to(self, tmp_path: Path) -> None:
        f = tmp_path / "python.instructions.md"
        f.write_text(
            "---\napplyTo: \"**/*.py\"\n---\nUse type hints everywhere.",
            encoding="utf-8",
        )
        inst = ManifestLoader(base_dir=tmp_path).load_instruction_manifest(f)
        assert inst.apply_to == ["**/*.py"]
        assert "type hints" in inst.body

    def test_apply_to_list(self, tmp_path: Path) -> None:
        f = tmp_path / "web.instructions.md"
        f.write_text(
            "---\napplyTo:\n  - \"**/*.ts\"\n  - \"**/*.tsx\"\n---\nUse React.",
            encoding="utf-8",
        )
        inst = ManifestLoader(base_dir=tmp_path).load_instruction_manifest(f)
        assert inst.apply_to == ["**/*.ts", "**/*.tsx"]

    def test_no_apply_to(self, tmp_path: Path) -> None:
        f = tmp_path / "general.md"
        f.write_text("Be concise.", encoding="utf-8")
        inst = ManifestLoader(base_dir=tmp_path).load_instruction_manifest(f)
        assert inst.apply_to == []


class TestLoadHooksFromJson:
    def test_basic_hooks(self, tmp_path: Path) -> None:
        f = tmp_path / "hooks.json"
        f.write_text(json.dumps({
            "hooks": {
                "preToolUse": [
                    {"type": "command", "command": "echo pre"},
                ],
                "postToolUse": [
                    {"type": "command", "command": "echo post"},
                ],
            }
        }), encoding="utf-8")
        hooks = ManifestLoader(base_dir=tmp_path).load_hooks_from_json(f)
        assert len(hooks) == 2
        assert hooks[0].event == "preToolUse"
        assert hooks[0].bash == "echo pre"
        assert hooks[1].event == "postToolUse"

    def test_flat_format(self, tmp_path: Path) -> None:
        """Also supports flat {event: entries} without wrapping 'hooks' key."""
        f = tmp_path / "hooks.json"
        f.write_text(json.dumps({
            "preToolUse": [{"command": "echo hi"}],
        }), encoding="utf-8")
        hooks = ManifestLoader(base_dir=tmp_path).load_hooks_from_json(f)
        assert len(hooks) == 1

    def test_missing_file(self, tmp_path: Path) -> None:
        assert ManifestLoader().load_hooks_from_json(tmp_path / "nope.json") == []


class TestLoadPermissions:
    def test_settings_json(self, tmp_path: Path) -> None:
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({
            "permissions": {
                "allow": ["Read", "Bash(git *)"],
                "deny": ["Bash(rm -rf /)"],
            }
        }), encoding="utf-8")
        perms = ManifestLoader(base_dir=tmp_path).load_permissions(f)
        assert "Read" in perms.allow
        assert "Bash(rm -rf /)" in perms.deny

    def test_missing_file(self, tmp_path: Path) -> None:
        perms = ManifestLoader().load_permissions(tmp_path / "nope.json")
        assert perms.allow == []
        assert perms.deny == []


class TestLoadMCPServerRefs:
    def test_mcp_json_format(self, tmp_path: Path) -> None:
        f = tmp_path / ".mcp.json"
        f.write_text(json.dumps({
            "mcpServers": {
                "github": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"GITHUB_TOKEN": "tok"},
                },
            }
        }), encoding="utf-8")
        refs = ManifestLoader(base_dir=tmp_path).load_mcp_server_refs(f)
        assert len(refs) == 1
        assert refs[0].name == "github"
        assert refs[0].command == "npx"
        assert refs[0].env == {"GITHUB_TOKEN": "tok"}

    def test_list_format(self, tmp_path: Path) -> None:
        f = tmp_path / "servers.json"
        f.write_text(json.dumps({
            "servers": [
                {"name": "mem", "command": "npx", "args": ["-y", "memory-server"]},
            ]
        }), encoding="utf-8")
        refs = ManifestLoader(base_dir=tmp_path).load_mcp_server_refs(f)
        assert len(refs) == 1
        assert refs[0].name == "mem"

    def test_missing_file(self, tmp_path: Path) -> None:
        assert ManifestLoader().load_mcp_server_refs(tmp_path / "nope.json") == []
