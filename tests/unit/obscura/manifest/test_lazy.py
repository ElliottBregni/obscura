"""Tests for lazy manifest proxy."""

from __future__ import annotations

from obscura.manifest.lazy import LazyField, LazyManifestProxy
from obscura.manifest.models import (
    AgentManifest,
    InstructionManifest,
    PermissionConfig,
    SkillManifest,
)


class TestLazyField:
    def test_resolves_once(self) -> None:
        call_count = 0

        def factory() -> str:
            nonlocal call_count
            call_count += 1
            return "value"

        field: LazyField[str] = LazyField(factory)
        assert not field.is_resolved
        assert field.get() == "value"
        assert field.is_resolved
        assert field.get() == "value"
        assert call_count == 1

    def test_invalidate_re_resolves(self) -> None:
        call_count = 0

        def factory() -> int:
            nonlocal call_count
            call_count += 1
            return call_count

        field: LazyField[int] = LazyField(factory)
        assert field.get() == 1
        field.invalidate()
        assert not field.is_resolved
        assert field.get() == 2


class TestLazyManifestProxy:
    def test_tool_policy(self) -> None:
        manifest = AgentManifest(
            name="dev",
            permissions=PermissionConfig(
                allow=["Read", "Bash"],
                deny=["Bash(rm *)"],
            ),
        )
        proxy = LazyManifestProxy(manifest)
        policy = proxy.tool_policy
        assert policy.name == "dev"
        assert "Read" in policy.allow_list
        assert "Bash(rm *)" in policy.deny_list

    def test_system_prompt_composition(self) -> None:
        manifest = AgentManifest(
            name="dev",
            system_prompt="You are a developer.",
            instructions=[
                InstructionManifest(body="Always use types."),
            ],
            skills=[
                SkillManifest(name="search", body="Search the web."),
            ],
        )
        proxy = LazyManifestProxy(manifest)
        prompt = proxy.system_prompt
        assert "You are a developer." in prompt
        assert "Always use types." in prompt
        assert "## Skill: search" in prompt

    def test_mcp_configs(self) -> None:
        from obscura.manifest.models import MCPServerRef

        manifest = AgentManifest(
            name="dev",
            mcp_server_refs=[
                MCPServerRef(
                    name="github",
                    command="npx",
                    args=["-y", "gh-server"],
                    env={"TOKEN": "tok"},
                ),
            ],
        )
        proxy = LazyManifestProxy(manifest)
        configs = proxy.mcp_configs
        assert len(configs) == 1
        assert configs[0]["command"] == "npx"
        assert configs[0]["env"] == {"TOKEN": "tok"}

    def test_skills_and_instructions(self) -> None:
        manifest = AgentManifest(
            name="dev",
            skills=[SkillManifest(name="s1", body="skill body")],
            instructions=[InstructionManifest(body="inst body")],
        )
        proxy = LazyManifestProxy(manifest)
        assert len(proxy.skills) == 1
        assert len(proxy.instructions) == 1

    def test_invalidate_all(self) -> None:
        manifest = AgentManifest(name="dev")
        proxy = LazyManifestProxy(manifest)
        # Access to trigger resolution
        _ = proxy.system_prompt
        _ = proxy.tool_policy
        assert proxy._system_prompt.is_resolved
        assert proxy._tool_policy.is_resolved
        proxy.invalidate_all()
        assert not proxy._system_prompt.is_resolved
        assert not proxy._tool_policy.is_resolved

    def test_hook_registry_empty(self) -> None:
        manifest = AgentManifest(name="dev")
        proxy = LazyManifestProxy(manifest)
        registry = proxy.hook_registry
        assert registry.count == 0

    def test_hook_registry_with_definitions(self) -> None:
        from obscura.manifest.models import HookDefinition

        manifest = AgentManifest(
            name="dev",
            hooks=[
                HookDefinition(event="preToolUse", bash="echo pre"),
                HookDefinition(event="postToolUse", bash="echo post"),
            ],
        )
        proxy = LazyManifestProxy(manifest)
        registry = proxy.hook_registry
        assert registry.count == 2
