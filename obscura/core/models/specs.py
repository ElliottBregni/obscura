"""Discriminated Pydantic union for compiler spec types.

The compiler reads five flavors of YAML manifest (Template, Agent,
Policy, Pack, Workspace), each with the same Kubernetes-flavored envelope
``{apiVersion, kind, metadata, spec}``. Internal callers should ``match``
on the variant rather than read ``spec.kind`` strings; the ``Spec`` type
alias is the union all five variants flow through.

Wire format is byte-identical with the previous ``BaseModel`` variants
that lived directly in ``core/compiler/specs.py`` — the discriminator
string values come from ``CompilerSpecKind`` and the alias on
``apiVersion`` is preserved so persisted YAML files keep parsing.

These models drop ``ObscuraModel``'s ``strict=True`` because YAML and
JSON both serialize sequences as lists — Pydantic strict mode rejects a
list when the field is annotated as ``tuple``, breaking every existing
spec file. Otherwise the configuration mirrors ``ObscuraModel``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from obscura.core.enums.tools import CompilerSpecKind


_SPEC_CONFIG = ConfigDict(
    frozen=True,
    extra="forbid",
    validate_assignment=True,
    use_enum_values=False,
    populate_by_name=True,
)


def _empty_str_list() -> list[str]:
    return []


def _empty_dict() -> dict[str, Any]:
    return {}


# ---------------------------------------------------------------------------
# Shared metadata
# ---------------------------------------------------------------------------


class SpecMetadata(BaseModel):
    """Common ``metadata`` block for all spec types."""

    model_config = _SPEC_CONFIG

    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=_empty_str_list)


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------


class MCPServerSpec(BaseModel):
    """MCP server reference within a template."""

    model_config = _SPEC_CONFIG

    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=_empty_str_list)
    env: dict[str, str] = Field(default_factory=dict)


class ToolRoutingSpec(BaseModel):
    """Tool routing configuration within a template spec."""

    model_config = _SPEC_CONFIG

    enabled: bool = True
    max_tools: int = 50
    pinned_tools: list[str] = Field(default_factory=_empty_str_list)
    pin_default_capabilities: bool = True
    use_quality_scores: bool = True
    use_context_recall: bool = True
    min_quality_score: float = 0.2
    capability_match_threshold: float = 0.3


class TemplateSpecBody(BaseModel):
    """The ``spec`` block of a Template."""

    model_config = _SPEC_CONFIG

    extends: str | None = None

    agent_type: str = "loop"
    max_iterations: int = 25
    provider: str = "copilot"
    model_id: str | None = None

    instructions: str = ""

    plugins: list[str] = Field(default_factory=_empty_str_list)
    capabilities: list[str] = Field(default_factory=_empty_str_list)

    tool_allowlist: list[str] | None = None
    tool_denylist: list[str] = Field(default_factory=_empty_str_list)

    tool_routing: ToolRoutingSpec | None = None

    mcp_servers: list[MCPServerSpec] = Field(default_factory=lambda: [])

    config: dict[str, Any] = Field(default_factory=_empty_dict)

    input_schema: dict[str, Any] | None = None


class TemplateSpec(BaseModel):
    """A reusable agent blueprint loaded from ``template.yml``."""

    model_config = _SPEC_CONFIG

    api_version: str = Field("obscura/v1", alias="apiVersion")
    kind: Literal[CompilerSpecKind.TEMPLATE] = CompilerSpecKind.TEMPLATE
    metadata: SpecMetadata
    spec: TemplateSpecBody


# ---------------------------------------------------------------------------
# Agent Instance
# ---------------------------------------------------------------------------


class AgentInstanceSpecBody(BaseModel):
    """The ``spec`` block of an Agent instance."""

    model_config = _SPEC_CONFIG

    template: str
    mode: str = "task"
    input: dict[str, Any] = Field(default_factory=_empty_dict)
    overrides: dict[str, Any] = Field(default_factory=_empty_dict)


class AgentInstanceSpec(BaseModel):
    """A concrete agent instance binding a template."""

    model_config = _SPEC_CONFIG

    api_version: str = Field("obscura/v1", alias="apiVersion")
    kind: Literal[CompilerSpecKind.AGENT] = CompilerSpecKind.AGENT
    metadata: SpecMetadata
    spec: AgentInstanceSpecBody


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class PolicySpecBody(BaseModel):
    """The ``spec`` block of a Policy."""

    model_config = _SPEC_CONFIG

    tool_allowlist: list[str] | None = None
    tool_denylist: list[str] = Field(default_factory=_empty_str_list)
    require_confirmation: list[str] = Field(default_factory=_empty_str_list)

    plugin_allowlist: list[str] | None = None
    plugin_denylist: list[str] = Field(default_factory=_empty_str_list)

    max_turns: int = 25
    token_budget: int = 0

    base_dir: str | None = None

    allow_dynamic_tools: bool = False


class PolicySpec(BaseModel):
    """Trust and execution rules loaded from ``policy.yml``."""

    model_config = _SPEC_CONFIG

    api_version: str = Field("obscura/v1", alias="apiVersion")
    kind: Literal[CompilerSpecKind.POLICY] = CompilerSpecKind.POLICY
    metadata: SpecMetadata
    spec: PolicySpecBody


# ---------------------------------------------------------------------------
# Pack
# ---------------------------------------------------------------------------


class CapabilityGrantSpec(BaseModel):
    """Capability grant/deny defaults for a pack."""

    model_config = _SPEC_CONFIG

    grant: list[str] = Field(default_factory=_empty_str_list)
    deny: list[str] = Field(default_factory=_empty_str_list)


class PackSpecBody(BaseModel):
    """The ``spec`` block of a Pack."""

    model_config = _SPEC_CONFIG

    plugins: list[str] = Field(default_factory=_empty_str_list)
    templates: list[str] = Field(default_factory=_empty_str_list)
    policies: list[str] = Field(default_factory=_empty_str_list)
    capabilities: CapabilityGrantSpec = Field(default_factory=CapabilityGrantSpec)
    config: dict[str, Any] = Field(default_factory=_empty_dict)
    instructions: str = ""


class PackSpec(BaseModel):
    """A curated bundle of plugins, templates, policies, and config."""

    model_config = _SPEC_CONFIG

    api_version: str = Field("obscura/v1", alias="apiVersion")
    kind: Literal[CompilerSpecKind.PACK] = CompilerSpecKind.PACK
    metadata: SpecMetadata
    spec: PackSpecBody


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


class PluginFilterSpec(BaseModel):
    """Plugin include/exclude lists for a workspace."""

    model_config = _SPEC_CONFIG

    include: list[str] = Field(default_factory=_empty_str_list)
    exclude: list[str] = Field(default_factory=_empty_str_list)


class MemoryBindingSpec(BaseModel):
    """Memory store binding for a workspace."""

    model_config = _SPEC_CONFIG

    namespace: str
    shared_scope: str = "workspace"
    stores: list[str] = Field(default_factory=_empty_str_list)
    retention_days: int = 30


class StartupSpec(BaseModel):
    """Startup behavior for a workspace."""

    model_config = _SPEC_CONFIG

    preload_plugins: bool = True
    start_agents: list[str] = Field(default_factory=_empty_str_list)


class WorkspaceAgentRef(BaseModel):
    """Inline agent definition within a workspace."""

    model_config = _SPEC_CONFIG

    name: str
    template: str
    mode: str = "task"
    input: dict[str, Any] = Field(default_factory=_empty_dict)
    overrides: dict[str, Any] = Field(default_factory=_empty_dict)


class WorkspaceSpecBody(BaseModel):
    """The ``spec`` block of a Workspace."""

    model_config = _SPEC_CONFIG

    packs: list[str] = Field(default_factory=_empty_str_list)
    config: dict[str, Any] = Field(default_factory=_empty_dict)
    policies: list[str] = Field(default_factory=_empty_str_list)
    plugins: PluginFilterSpec = Field(default_factory=PluginFilterSpec)
    memory: MemoryBindingSpec | None = None
    agents: list[WorkspaceAgentRef] = Field(default_factory=lambda: [])
    startup: StartupSpec = Field(default_factory=StartupSpec)


class WorkspaceSpec(BaseModel):
    """Top-level workspace specification — the runtime entrypoint."""

    model_config = _SPEC_CONFIG

    api_version: str = Field("obscura/v1", alias="apiVersion")
    kind: Literal[CompilerSpecKind.WORKSPACE] = CompilerSpecKind.WORKSPACE
    metadata: SpecMetadata
    spec: WorkspaceSpecBody


# ---------------------------------------------------------------------------
# Discriminated union
# ---------------------------------------------------------------------------


Spec = Annotated[
    Union[
        TemplateSpec,
        AgentInstanceSpec,
        PolicySpec,
        PackSpec,
        WorkspaceSpec,
    ],
    Field(discriminator="kind"),
]
"""Discriminated union over the five spec variants."""


# Legacy alias retained so callers that imported ``AnySpec`` keep working.
AnySpec = Spec


SPEC_KIND_MAP: dict[str, type[Any]] = {
    CompilerSpecKind.TEMPLATE.value: TemplateSpec,
    CompilerSpecKind.AGENT.value: AgentInstanceSpec,
    CompilerSpecKind.POLICY.value: PolicySpec,
    CompilerSpecKind.PACK.value: PackSpec,
    CompilerSpecKind.WORKSPACE.value: WorkspaceSpec,
}


__all__ = [
    "AgentInstanceSpec",
    "AgentInstanceSpecBody",
    "AnySpec",
    "CapabilityGrantSpec",
    "CompilerSpecKind",
    "MCPServerSpec",
    "MemoryBindingSpec",
    "PackSpec",
    "PackSpecBody",
    "PluginFilterSpec",
    "PolicySpec",
    "PolicySpecBody",
    "SPEC_KIND_MAP",
    "Spec",
    "SpecMetadata",
    "StartupSpec",
    "TemplateSpec",
    "TemplateSpecBody",
    "ToolRoutingSpec",
    "WorkspaceAgentRef",
    "WorkspaceSpec",
    "WorkspaceSpecBody",
]
