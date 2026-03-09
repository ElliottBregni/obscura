"""Tests for obscura.core.compiler.compile — End-to-end compile pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from obscura.core.compiler.compile import (
    compile_workspace_from_dir,
    compile_workspace_from_registry,
    synthesize_implicit_workspace,
)
from obscura.core.compiler.compiled import CompiledWorkspace
from obscura.core.compiler.errors import ResolutionError, SpecValidationError
from obscura.core.compiler.loader import SpecRegistry
from obscura.core.compiler.specs import (
    PolicySpec,
    PolicySpecBody,
    SpecMetadata,
    TemplateSpec,
    TemplateSpecBody,
    WorkspaceAgentRef,
    WorkspaceSpec,
    WorkspaceSpecBody,
    PluginFilterSpec,
    MemoryBindingSpec,
    StartupSpec,
)


def _write_yaml(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


class TestCompileWorkspaceFromDir:
    def test_full_pipeline(self, tmp_path: Path) -> None:
        specs = tmp_path / "specs"
        specs.mkdir()

        _write_yaml(
            specs / "base-agent.yml",
            """\
apiVersion: obscura/v1
kind: Template
metadata:
  name: base-agent
spec:
  provider: copilot
  plugins:
    - shell
  instructions: |
    You are a helpful agent.
""",
        )

        _write_yaml(
            specs / "code-agent.yml",
            """\
apiVersion: obscura/v1
kind: Template
metadata:
  name: code-agent
  tags: [dev]
spec:
  extends: base-agent
  provider: claude
  plugins:
    - git
    - tests
  instructions: |
    Focus on code quality.
""",
        )

        _write_yaml(
            specs / "safe-dev.yml",
            """\
apiVersion: obscura/v1
kind: Policy
metadata:
  name: safe-dev
spec:
  tool_denylist:
    - dangerous_tool
  require_confirmation:
    - bash
  max_turns: 15
""",
        )

        _write_yaml(
            specs / "code-mode.yml",
            """\
apiVersion: obscura/v1
kind: Workspace
metadata:
  name: code-mode
spec:
  config:
    default_backend: claude
  policies:
    - safe-dev
  plugins:
    include:
      - shell
      - git
      - tests
    exclude:
      - prod-kubectl
  memory:
    namespace: code-mode
    stores:
      - vector
      - state
  agents:
    - name: dev
      template: code-agent
      mode: task
      input:
        repo_path: .
    - name: daemon
      template: code-agent
      mode: daemon
  startup:
    preload_plugins: false
    start_agents:
      - daemon
""",
        )

        ws = compile_workspace_from_dir("code-mode", specs)

        assert isinstance(ws, CompiledWorkspace)
        assert ws.name == "code-mode"

        # Agents
        assert len(ws.agents) == 2
        dev = next(a for a in ws.agents if a.name == "dev")
        daemon = next(a for a in ws.agents if a.name == "daemon")

        assert dev.mode == "task"
        assert dev.provider == "claude"
        assert dev.template_name == "code-agent"
        assert dev.input_vars == {"repo_path": "."}
        # Inherited from base + code-agent, filtered by workspace include
        assert "shell" in dev.plugins
        assert "git" in dev.plugins
        assert "tests" in dev.plugins

        assert daemon.mode == "daemon"

        # Instructions merged (base + child)
        assert "helpful agent" in dev.instructions
        assert "code quality" in dev.instructions

        # Policy applied
        assert "dangerous_tool" in dev.tool_denylist

        # Policies
        assert len(ws.policies) == 1
        assert ws.policies[0].name == "safe-dev"
        assert ws.policies[0].max_turns == 15

        # Memory
        assert ws.memory is not None
        assert ws.memory.namespace == "code-mode"

        # Startup
        assert ws.startup_agents == ("daemon",)
        assert ws.preload_plugins is False

        # Config
        assert ws.config["default_backend"] == "claude"

    def test_missing_workspace(self, tmp_path: Path) -> None:
        specs = tmp_path / "specs"
        specs.mkdir()
        with pytest.raises(ResolutionError, match="not found"):
            compile_workspace_from_dir("nonexistent", specs)


class TestCompileWorkspaceFromRegistry:
    def test_agent_overrides_applied(self) -> None:
        registry = SpecRegistry()
        registry.add(TemplateSpec(
            metadata=SpecMetadata(name="base"),
            spec=TemplateSpecBody(
                provider="copilot",
                plugins=["git"],
                config={"shell": {"cwd": "/tmp"}},
            ),
        ))
        registry.add(PolicySpec(
            metadata=SpecMetadata(name="p"),
            spec=PolicySpecBody(),
        ))

        ws = WorkspaceSpec(
            metadata=SpecMetadata(name="ws"),
            spec=WorkspaceSpecBody(
                policies=["p"],
                agents=[
                    WorkspaceAgentRef(
                        name="dev",
                        template="base",
                        overrides={
                            "provider": "claude",
                            "plugins": ["tests"],
                            "config": {"shell": {"timeout": 30}},
                        },
                    ),
                ],
            ),
        )
        registry.add(ws)

        compiled = compile_workspace_from_registry(ws, registry)
        agent = compiled.agents[0]
        assert agent.provider == "claude"
        assert "git" in agent.plugins
        assert "tests" in agent.plugins
        assert agent.config == {"shell": {"cwd": "/tmp", "timeout": 30}}

    def test_strict_validation_raises(self) -> None:
        registry = SpecRegistry()
        registry.add(TemplateSpec(
            metadata=SpecMetadata(name="t"),
            spec=TemplateSpecBody(),
        ))

        ws = WorkspaceSpec(
            metadata=SpecMetadata(name="ws"),
            spec=WorkspaceSpecBody(
                agents=[
                    WorkspaceAgentRef(
                        name="a",
                        template="t",
                        mode="task",
                    ),
                ],
                startup=StartupSpec(start_agents=["missing"]),
            ),
        )
        registry.add(ws)

        with pytest.raises(SpecValidationError, match="validation error"):
            compile_workspace_from_registry(ws, registry, strict=True)

    def test_non_strict_logs_warnings(self) -> None:
        registry = SpecRegistry()
        registry.add(TemplateSpec(
            metadata=SpecMetadata(name="t"),
            spec=TemplateSpecBody(),
        ))

        ws = WorkspaceSpec(
            metadata=SpecMetadata(name="ws"),
            spec=WorkspaceSpecBody(
                agents=[
                    WorkspaceAgentRef(name="a", template="t"),
                ],
                startup=StartupSpec(start_agents=["missing"]),
            ),
        )
        registry.add(ws)

        # Should not raise
        compiled = compile_workspace_from_registry(ws, registry, strict=False)
        assert compiled.name == "ws"


class TestSynthesizeImplicitWorkspace:
    def test_creates_default_workspace(self) -> None:
        ws = synthesize_implicit_workspace()
        assert ws.metadata.name == "default"
        assert ws.kind == "Workspace"
        assert len(ws.spec.agents) == 1
        assert ws.spec.agents[0].template == "implicit"

    def test_custom_name(self) -> None:
        ws = synthesize_implicit_workspace(name="custom")
        assert ws.metadata.name == "custom"
