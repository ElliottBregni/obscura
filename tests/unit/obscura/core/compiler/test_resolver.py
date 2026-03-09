"""Tests for obscura.core.compiler.resolver — Reference resolution."""

from __future__ import annotations

import pytest

from obscura.core.compiler.errors import ResolutionError
from obscura.core.compiler.loader import SpecRegistry
from obscura.core.compiler.resolver import (
    resolve_template_chain,
    resolve_workspace_agent_template,
    resolve_workspace_policies,
)
from obscura.core.compiler.specs import (
    PolicySpec,
    PolicySpecBody,
    SpecMetadata,
    TemplateSpec,
    TemplateSpecBody,
    WorkspaceAgentRef,
    WorkspaceSpec,
    WorkspaceSpecBody,
)


def _template(name: str, *, extends: str | None = None) -> TemplateSpec:
    return TemplateSpec(
        metadata=SpecMetadata(name=name),
        spec=TemplateSpecBody(extends=extends),
    )


def _policy(name: str) -> PolicySpec:
    return PolicySpec(
        metadata=SpecMetadata(name=name),
        spec=PolicySpecBody(),
    )


def _workspace(
    name: str,
    *,
    policies: list[str] | None = None,
    agents: list[WorkspaceAgentRef] | None = None,
) -> WorkspaceSpec:
    return WorkspaceSpec(
        metadata=SpecMetadata(name=name),
        spec=WorkspaceSpecBody(
            policies=policies or [],
            agents=agents or [],
        ),
    )


class TestResolveTemplateChain:
    def test_no_extends(self) -> None:
        t = _template("base")
        registry = SpecRegistry()
        registry.add(t)
        chain = resolve_template_chain(t, registry)
        assert chain == [t]

    def test_single_extends(self) -> None:
        base = _template("base")
        child = _template("child", extends="base")
        registry = SpecRegistry()
        registry.add(base)
        registry.add(child)
        chain = resolve_template_chain(child, registry)
        assert len(chain) == 2
        assert chain[0].metadata.name == "base"
        assert chain[1].metadata.name == "child"

    def test_missing_parent(self) -> None:
        child = _template("child", extends="missing")
        registry = SpecRegistry()
        registry.add(child)
        with pytest.raises(ResolutionError, match="not found"):
            resolve_template_chain(child, registry)

    def test_deep_inheritance_rejected(self) -> None:
        grandparent = _template("gp", extends="ancestor")
        child = _template("child", extends="gp")
        registry = SpecRegistry()
        registry.add(grandparent)
        registry.add(child)
        with pytest.raises(ResolutionError, match="Max inheritance depth"):
            resolve_template_chain(child, registry)


class TestResolveWorkspacePolicies:
    def test_resolves_all(self) -> None:
        p1 = _policy("p1")
        p2 = _policy("p2")
        ws = _workspace("ws", policies=["p1", "p2"])
        registry = SpecRegistry()
        registry.add(p1)
        registry.add(p2)
        registry.add(ws)
        policies = resolve_workspace_policies(ws, registry)
        assert len(policies) == 2
        assert policies[0].metadata.name == "p1"

    def test_missing_policy(self) -> None:
        ws = _workspace("ws", policies=["missing"])
        registry = SpecRegistry()
        registry.add(ws)
        with pytest.raises(ResolutionError, match="not found"):
            resolve_workspace_policies(ws, registry)


class TestResolveWorkspaceAgentTemplate:
    def test_resolves(self) -> None:
        tmpl = _template("code-agent")
        ref = WorkspaceAgentRef(name="dev", template="code-agent")
        registry = SpecRegistry()
        registry.add(tmpl)
        result = resolve_workspace_agent_template(ref, registry, "ws")
        assert result.metadata.name == "code-agent"

    def test_missing_template(self) -> None:
        ref = WorkspaceAgentRef(name="dev", template="missing")
        registry = SpecRegistry()
        with pytest.raises(ResolutionError, match="not found"):
            resolve_workspace_agent_template(ref, registry, "ws")
