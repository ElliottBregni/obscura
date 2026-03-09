"""Tests for obscura.core.compiler.specs — Pydantic spec models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from obscura.core.compiler.specs import (
    AgentInstanceSpec,
    MCPServerSpec,
    MemoryBindingSpec,
    PluginFilterSpec,
    PolicySpec,
    SpecMetadata,
    StartupSpec,
    TemplateSpec,
    TemplateSpecBody,
    WorkspaceAgentRef,
    WorkspaceSpec,
    WorkspaceSpecBody,
    SPEC_KIND_MAP,
)


class TestSpecMetadata:
    def test_minimal(self) -> None:
        meta = SpecMetadata(name="test")
        assert meta.name == "test"
        assert meta.description == ""
        assert meta.tags == []

    def test_full(self) -> None:
        meta = SpecMetadata(name="test", description="A test", tags=["a", "b"])
        assert meta.tags == ["a", "b"]

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            SpecMetadata(name="test", unknown_field="bad")  # type: ignore[call-arg]


class TestTemplateSpec:
    def test_minimal(self) -> None:
        t = TemplateSpec(
            metadata=SpecMetadata(name="base"),
            spec=TemplateSpecBody(),
        )
        assert t.kind == "Template"
        assert t.api_version == "obscura/v1"
        assert t.metadata.name == "base"
        assert t.spec.agent_type == "loop"
        assert t.spec.provider == "copilot"
        assert t.spec.extends is None
        assert t.spec.plugins == []
        assert t.spec.tool_allowlist is None

    def test_full(self) -> None:
        t = TemplateSpec(
            metadata=SpecMetadata(
                name="code-agent",
                description="Code-focused agent",
                tags=["dev", "coding"],
            ),
            spec=TemplateSpecBody(
                extends="base-agent",
                agent_type="aper",
                max_iterations=20,
                provider="claude",
                model_id="sonnet",
                instructions="You are a code agent.",
                plugins=["git", "shell"],
                capabilities=["code-analysis"],
                tool_allowlist=["read_file", "write_file"],
                tool_denylist=["dangerous"],
                mcp_servers=[MCPServerSpec(name="test", command="node")],
                config={"shell": {"cwd": "/tmp"}},
                input_schema={"type": "object"},
            ),
        )
        assert t.spec.extends == "base-agent"
        assert t.spec.plugins == ["git", "shell"]
        assert t.spec.tool_allowlist == ["read_file", "write_file"]
        assert len(t.spec.mcp_servers) == 1

    def test_from_dict_with_alias(self) -> None:
        raw = {
            "apiVersion": "obscura/v1",
            "kind": "Template",
            "metadata": {"name": "test"},
            "spec": {},
        }
        t = TemplateSpec.model_validate(raw)
        assert t.api_version == "obscura/v1"
        assert t.metadata.name == "test"

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            TemplateSpec(
                metadata=SpecMetadata(name="test"),
                spec=TemplateSpecBody(),
                extra_field="bad",  # type: ignore[call-arg]
            )


class TestAgentInstanceSpec:
    def test_minimal(self) -> None:
        from obscura.core.compiler.specs import AgentInstanceSpecBody
        a = AgentInstanceSpec(
            metadata=SpecMetadata(name="my-agent"),
            spec=AgentInstanceSpecBody(template="code-agent"),
        )
        assert a.kind == "Agent"
        assert a.spec.template == "code-agent"
        assert a.spec.mode == "task"
        assert a.spec.input == {}
        assert a.spec.overrides == {}

    def test_with_overrides(self) -> None:
        from obscura.core.compiler.specs import AgentInstanceSpecBody
        a = AgentInstanceSpec(
            metadata=SpecMetadata(name="orders-dev"),
            spec=AgentInstanceSpecBody(
                template="code-agent",
                mode="daemon",
                input={"repo_path": "./services/orders"},
                overrides={"plugins": ["postgres"]},
            ),
        )
        assert a.spec.mode == "daemon"
        assert a.spec.input["repo_path"] == "./services/orders"


class TestPolicySpec:
    def test_defaults(self) -> None:
        from obscura.core.compiler.specs import PolicySpecBody
        p = PolicySpec(
            metadata=SpecMetadata(name="default"),
            spec=PolicySpecBody(),
        )
        assert p.spec.tool_allowlist is None
        assert p.spec.tool_denylist == []
        assert p.spec.max_turns == 25
        assert p.spec.allow_dynamic_tools is False

    def test_full(self) -> None:
        from obscura.core.compiler.specs import PolicySpecBody
        p = PolicySpec(
            metadata=SpecMetadata(name="strict"),
            spec=PolicySpecBody(
                tool_denylist=["bash", "delete_file"],
                require_confirmation=["write_file"],
                plugin_denylist=["prod-kubectl"],
                max_turns=10,
                token_budget=50000,
                base_dir="/tmp/safe",
            ),
        )
        assert p.spec.tool_denylist == ["bash", "delete_file"]
        assert p.spec.base_dir == "/tmp/safe"


class TestWorkspaceSpec:
    def test_minimal(self) -> None:
        ws = WorkspaceSpec(
            metadata=SpecMetadata(name="default"),
            spec=WorkspaceSpecBody(),
        )
        assert ws.kind == "Workspace"
        assert ws.spec.agents == []
        assert ws.spec.policies == []
        assert ws.spec.plugins.include == []
        assert ws.spec.memory is None
        assert ws.spec.startup.preload_plugins is True

    def test_full(self) -> None:
        ws = WorkspaceSpec(
            metadata=SpecMetadata(name="code-mode"),
            spec=WorkspaceSpecBody(
                config={"default_backend": "claude"},
                policies=["safe-dev"],
                plugins=PluginFilterSpec(
                    include=["git", "shell"],
                    exclude=["prod-kubectl"],
                ),
                memory=MemoryBindingSpec(
                    namespace="code-mode",
                    stores=["vector", "state"],
                ),
                agents=[
                    WorkspaceAgentRef(
                        name="repo-daemon",
                        template="code-agent",
                        mode="daemon",
                        input={"repo_path": "."},
                    ),
                ],
                startup=StartupSpec(
                    preload_plugins=False,
                    start_agents=["repo-daemon"],
                ),
            ),
        )
        assert len(ws.spec.agents) == 1
        assert ws.spec.agents[0].name == "repo-daemon"
        assert ws.spec.memory is not None
        assert ws.spec.memory.namespace == "code-mode"


class TestSpecKindMap:
    def test_all_kinds_registered(self) -> None:
        assert "Template" in SPEC_KIND_MAP
        assert "Agent" in SPEC_KIND_MAP
        assert "Policy" in SPEC_KIND_MAP
        assert "Workspace" in SPEC_KIND_MAP
        assert len(SPEC_KIND_MAP) == 4
