"""Tests for obscura.core.compiler.merger — Spec merging and compilation."""

from __future__ import annotations

from pathlib import Path

from obscura.core.compiler.compiled import CompiledPolicy
from obscura.core.compiler.merger import (
    apply_agent_overrides,
    compile_agent,
    compile_memory,
    compile_policy,
    merge_template_chain,
    _deep_merge,
    _merge_instructions,
    _merge_str_lists,
)
from obscura.core.compiler.specs import (
    MCPServerSpec,
    MemoryBindingSpec,
    PolicySpec,
    PolicySpecBody,
    SpecMetadata,
    TemplateSpec,
    TemplateSpecBody,
    WorkspaceAgentRef,
    WorkspaceSpec,
    WorkspaceSpecBody,
)


def _template(
    name: str,
    *,
    extends: str | None = None,
    plugins: list[str] | None = None,
    capabilities: list[str] | None = None,
    provider: str = "copilot",
    instructions: str = "",
    config: dict | None = None,
    model_id: str | None = None,
    mcp_servers: list[MCPServerSpec] | None = None,
) -> TemplateSpec:
    return TemplateSpec(
        metadata=SpecMetadata(name=name),
        spec=TemplateSpecBody(
            extends=extends,
            plugins=plugins or [],
            capabilities=capabilities or [],
            provider=provider,
            instructions=instructions,
            config=config or {},
            model_id=model_id,
            mcp_servers=mcp_servers or [],
        ),
    )


class TestMergeTemplateCHain:
    def test_single_template(self) -> None:
        t = _template("base", plugins=["git"])
        result = merge_template_chain([t])
        assert result is t

    def test_child_inherits_plugins(self) -> None:
        base = _template("base", plugins=["git", "shell"])
        child = _template("child", extends="base", plugins=["tests"])
        result = merge_template_chain([base, child])
        assert result.spec.plugins == ["git", "shell", "tests"]

    def test_child_overrides_provider(self) -> None:
        base = _template("base", provider="copilot")
        child = _template("child", extends="base", provider="claude")
        result = merge_template_chain([base, child])
        assert result.spec.provider == "claude"

    def test_child_inherits_model_id(self) -> None:
        base = _template("base", model_id="sonnet")
        child = _template("child", extends="base")
        result = merge_template_chain([base, child])
        assert result.spec.model_id == "sonnet"

    def test_child_overrides_model_id(self) -> None:
        base = _template("base", model_id="sonnet")
        child = _template("child", extends="base", model_id="opus")
        result = merge_template_chain([base, child])
        assert result.spec.model_id == "opus"

    def test_instructions_merged(self) -> None:
        base = _template("base", instructions="Base rules.")
        child = _template("child", extends="base", instructions="Child rules.")
        result = merge_template_chain([base, child])
        assert "Base rules." in result.spec.instructions
        assert "Child rules." in result.spec.instructions

    def test_config_deep_merged(self) -> None:
        base = _template("base", config={"shell": {"cwd": "/tmp"}, "debug": True})
        child = _template("child", extends="base", config={"shell": {"timeout": 30}})
        result = merge_template_chain([base, child])
        assert result.spec.config == {
            "shell": {"cwd": "/tmp", "timeout": 30},
            "debug": True,
        }

    def test_plugins_deduped(self) -> None:
        base = _template("base", plugins=["git", "shell"])
        child = _template("child", extends="base", plugins=["git", "tests"])
        result = merge_template_chain([base, child])
        assert result.spec.plugins == ["git", "shell", "tests"]

    def test_mcp_servers_merged_by_name(self) -> None:
        base = _template(
            "base",
            mcp_servers=[MCPServerSpec(name="a", command="cmd_a")],
        )
        child = _template(
            "child",
            extends="base",
            mcp_servers=[
                MCPServerSpec(name="a", command="cmd_a_override"),
                MCPServerSpec(name="b", command="cmd_b"),
            ],
        )
        result = merge_template_chain([base, child])
        servers = {s.name: s for s in result.spec.mcp_servers}
        assert servers["a"].command == "cmd_a_override"
        assert "b" in servers

    def test_extends_cleared(self) -> None:
        base = _template("base")
        child = _template("child", extends="base")
        result = merge_template_chain([base, child])
        assert result.spec.extends is None

    def test_metadata_from_child(self) -> None:
        base = _template("base")
        base = TemplateSpec(
            metadata=SpecMetadata(name="base", description="Base desc"),
            spec=TemplateSpecBody(),
        )
        child = TemplateSpec(
            metadata=SpecMetadata(name="child", description="Child desc"),
            spec=TemplateSpecBody(extends="base"),
        )
        result = merge_template_chain([base, child])
        assert result.metadata.name == "child"
        assert result.metadata.description == "Child desc"


class TestApplyAgentOverrides:
    def test_no_overrides(self) -> None:
        t = _template("base", plugins=["git"])
        ref = WorkspaceAgentRef(name="dev", template="base")
        result = apply_agent_overrides(t, ref)
        assert result is t

    def test_plugin_override(self) -> None:
        t = _template("base", plugins=["git"])
        ref = WorkspaceAgentRef(
            name="dev",
            template="base",
            overrides={"plugins": ["postgres"]},
        )
        result = apply_agent_overrides(t, ref)
        assert result.spec.plugins == ["git", "postgres"]

    def test_config_override(self) -> None:
        t = _template("base", config={"shell": {"cwd": "/tmp"}})
        ref = WorkspaceAgentRef(
            name="dev",
            template="base",
            overrides={"config": {"shell": {"timeout": 30}}},
        )
        result = apply_agent_overrides(t, ref)
        assert result.spec.config == {"shell": {"cwd": "/tmp", "timeout": 30}}

    def test_scalar_override(self) -> None:
        t = _template("base", provider="copilot")
        ref = WorkspaceAgentRef(
            name="dev",
            template="base",
            overrides={"provider": "claude"},
        )
        result = apply_agent_overrides(t, ref)
        assert result.spec.provider == "claude"


class TestCompilePolicy:
    def test_compiles_defaults(self) -> None:
        p = PolicySpec(
            metadata=SpecMetadata(name="default"),
            spec=PolicySpecBody(),
        )
        compiled = compile_policy(p)
        assert compiled.name == "default"
        assert compiled.tool_allowlist is None
        assert compiled.tool_denylist == frozenset()
        assert compiled.max_turns == 25

    def test_compiles_full(self) -> None:
        p = PolicySpec(
            metadata=SpecMetadata(name="strict"),
            spec=PolicySpecBody(
                tool_denylist=["bash"],
                require_confirmation=["write_file"],
                base_dir="/safe",
                max_turns=10,
                token_budget=50000,
            ),
        )
        compiled = compile_policy(p)
        assert "bash" in compiled.tool_denylist
        assert "write_file" in compiled.require_confirmation
        assert compiled.base_dir == Path("/safe")
        assert compiled.max_turns == 10


class TestCompileAgent:
    def test_basic_compile(self) -> None:
        t = _template("code-agent", plugins=["git", "shell"], provider="claude")
        ref = WorkspaceAgentRef(name="dev", template="code-agent", mode="task")
        agent = compile_agent(t, ref, [], ([], []))
        assert agent.name == "dev"
        assert agent.template_name == "code-agent"
        assert agent.provider == "claude"
        assert agent.plugins == ("git", "shell")
        assert agent.mode == "task"

    def test_workspace_plugin_include_filters(self) -> None:
        t = _template("t", plugins=["git", "shell", "github"])
        ref = WorkspaceAgentRef(name="a", template="t")
        agent = compile_agent(t, ref, [], (["git", "shell"], []))
        assert agent.plugins == ("git", "shell")

    def test_workspace_plugin_exclude_filters(self) -> None:
        t = _template("t", plugins=["git", "shell", "prod-kubectl"])
        ref = WorkspaceAgentRef(name="a", template="t")
        agent = compile_agent(t, ref, [], ([], ["prod-kubectl"]))
        assert "prod-kubectl" not in agent.plugins

    def test_policy_filters_plugins(self) -> None:
        t = _template("t", plugins=["git", "dangerous"])
        ref = WorkspaceAgentRef(name="a", template="t")
        policy = CompiledPolicy(
            name="safe",
            plugin_denylist=frozenset(["dangerous"]),
        )
        agent = compile_agent(t, ref, [policy], ([], []))
        assert "dangerous" not in agent.plugins

    def test_policy_filters_tools(self) -> None:
        t = _template("t")
        t = TemplateSpec(
            metadata=SpecMetadata(name="t"),
            spec=TemplateSpecBody(
                tool_allowlist=["read_file", "write_file", "bash"],
            ),
        )
        ref = WorkspaceAgentRef(name="a", template="t")
        policy = CompiledPolicy(
            name="safe",
            tool_allowlist=frozenset(["read_file", "write_file"]),
            tool_denylist=frozenset(["bash"]),
        )
        agent = compile_agent(t, ref, [policy], ([], []))
        assert agent.tool_allowlist == frozenset(["read_file", "write_file"])
        assert "bash" in agent.tool_denylist

    def test_input_vars_passed(self) -> None:
        t = _template("t")
        ref = WorkspaceAgentRef(
            name="a",
            template="t",
            input={"repo_path": "./src"},
        )
        agent = compile_agent(t, ref, [], ([], []))
        assert agent.input_vars == {"repo_path": "./src"}


class TestCompileMemory:
    def test_none_when_not_set(self) -> None:
        ws = WorkspaceSpec(
            metadata=SpecMetadata(name="ws"),
            spec=WorkspaceSpecBody(),
        )
        assert compile_memory(ws) is None

    def test_compiles_memory(self) -> None:
        ws = WorkspaceSpec(
            metadata=SpecMetadata(name="ws"),
            spec=WorkspaceSpecBody(
                memory=MemoryBindingSpec(
                    namespace="test",
                    stores=["vector", "state"],
                    retention_days=7,
                ),
            ),
        )
        mem = compile_memory(ws)
        assert mem is not None
        assert mem.namespace == "test"
        assert mem.stores == ("vector", "state")
        assert mem.retention_days == 7


class TestHelpers:
    def test_merge_str_lists_dedupes(self) -> None:
        assert _merge_str_lists(["a", "b"], ["b", "c"]) == ["a", "b", "c"]

    def test_merge_str_lists_preserves_order(self) -> None:
        assert _merge_str_lists(["b", "a"], ["c"]) == ["b", "a", "c"]

    def test_merge_instructions_both(self) -> None:
        result = _merge_instructions("Base.", "Child.")
        assert "Base." in result
        assert "Child." in result

    def test_merge_instructions_empty_base(self) -> None:
        assert _merge_instructions("", "Child.") == "Child."

    def test_merge_instructions_empty_child(self) -> None:
        assert _merge_instructions("Base.", "") == "Base."

    def test_deep_merge(self) -> None:
        result = _deep_merge(
            {"a": 1, "b": {"c": 2, "d": 3}},
            {"b": {"c": 99, "e": 4}, "f": 5},
        )
        assert result == {"a": 1, "b": {"c": 99, "d": 3, "e": 4}, "f": 5}
