"""obscura.core.compiler.specs — Pydantic models for declarative YAML specs.

These models represent the raw, user-authored YAML files:
  - TemplateSpec:  reusable agent blueprint
  - AgentInstanceSpec:  concrete agent binding template + input
  - PolicySpec:  trust and execution rules
  - WorkspaceSpec:  bundle of everything into one mode

All specs follow a Kubernetes-like envelope: apiVersion, kind, metadata, spec.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


def _empty_str_list() -> list[str]:
    return []


def _empty_dict() -> dict[str, Any]:
    return {}


# ---------------------------------------------------------------------------
# Shared metadata
# ---------------------------------------------------------------------------


class SpecMetadata(BaseModel):
    """Common metadata block for all spec types."""

    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=_empty_str_list)

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------


class MCPServerSpec(BaseModel):
    """MCP server reference within a template."""

    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=_empty_str_list)
    env: dict[str, str] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class TemplateSpecBody(BaseModel):
    """The ``spec`` block of a Template."""

    extends: str | None = None

    # Runtime defaults
    agent_type: str = "loop"
    max_iterations: int = 25
    provider: str = "copilot"
    model_id: str | None = None

    # Instructions (system prompt body)
    instructions: str = ""

    # Plugins and capabilities the template wants
    plugins: list[str] = Field(default_factory=_empty_str_list)
    capabilities: list[str] = Field(default_factory=_empty_str_list)

    # Tool access
    tool_allowlist: list[str] | None = None
    tool_denylist: list[str] = Field(default_factory=_empty_str_list)

    # MCP servers
    mcp_servers: list[MCPServerSpec] = Field(default_factory=list)

    # Config defaults passed to the agent runtime
    config: dict[str, Any] = Field(default_factory=_empty_dict)

    # Optional JSON Schema describing expected input variables
    input_schema: dict[str, Any] | None = None

    model_config = {"extra": "forbid"}


class TemplateSpec(BaseModel):
    """A reusable agent blueprint loaded from ``template.yml``."""

    api_version: str = Field("obscura/v1", alias="apiVersion")
    kind: Literal["Template"] = "Template"
    metadata: SpecMetadata
    spec: TemplateSpecBody

    model_config = {"populate_by_name": True, "extra": "forbid"}


# ---------------------------------------------------------------------------
# Agent Instance
# ---------------------------------------------------------------------------


class AgentInstanceSpecBody(BaseModel):
    """The ``spec`` block of an Agent instance."""

    template: str
    mode: str = "task"  # task | daemon | reactive | scheduled
    input: dict[str, Any] = Field(default_factory=_empty_dict)
    overrides: dict[str, Any] = Field(default_factory=_empty_dict)

    model_config = {"extra": "forbid"}


class AgentInstanceSpec(BaseModel):
    """A concrete agent instance binding a template."""

    api_version: str = Field("obscura/v1", alias="apiVersion")
    kind: Literal["Agent"] = "Agent"
    metadata: SpecMetadata
    spec: AgentInstanceSpecBody

    model_config = {"populate_by_name": True, "extra": "forbid"}


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class PolicySpecBody(BaseModel):
    """The ``spec`` block of a Policy."""

    # Tool access
    tool_allowlist: list[str] | None = None
    tool_denylist: list[str] = Field(default_factory=_empty_str_list)
    require_confirmation: list[str] = Field(default_factory=_empty_str_list)

    # Plugin access
    plugin_allowlist: list[str] | None = None
    plugin_denylist: list[str] = Field(default_factory=_empty_str_list)

    # Budgets
    max_turns: int = 25
    token_budget: int = 0

    # Filesystem restriction
    base_dir: str | None = None

    # Dynamic tool loading
    allow_dynamic_tools: bool = False

    model_config = {"extra": "forbid"}


class PolicySpec(BaseModel):
    """Trust and execution rules loaded from ``policy.yml``."""

    api_version: str = Field("obscura/v1", alias="apiVersion")
    kind: Literal["Policy"] = "Policy"
    metadata: SpecMetadata
    spec: PolicySpecBody

    model_config = {"populate_by_name": True, "extra": "forbid"}


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


class PluginFilterSpec(BaseModel):
    """Plugin include/exclude lists for a workspace."""

    include: list[str] = Field(default_factory=_empty_str_list)
    exclude: list[str] = Field(default_factory=_empty_str_list)

    model_config = {"extra": "forbid"}


class MemoryBindingSpec(BaseModel):
    """Memory store binding for a workspace."""

    namespace: str
    shared_scope: str = "workspace"  # workspace | agent | session
    stores: list[str] = Field(default_factory=_empty_str_list)
    retention_days: int = 30

    model_config = {"extra": "forbid"}


class StartupSpec(BaseModel):
    """Startup behavior for a workspace."""

    preload_plugins: bool = True
    start_agents: list[str] = Field(default_factory=_empty_str_list)

    model_config = {"extra": "forbid"}


class WorkspaceAgentRef(BaseModel):
    """Inline agent definition within a workspace (shorthand for AgentInstanceSpec)."""

    name: str
    template: str
    mode: str = "task"
    input: dict[str, Any] = Field(default_factory=_empty_dict)
    overrides: dict[str, Any] = Field(default_factory=_empty_dict)

    model_config = {"extra": "forbid"}


class WorkspaceSpecBody(BaseModel):
    """The ``spec`` block of a Workspace."""

    # Runtime config overrides
    config: dict[str, Any] = Field(default_factory=_empty_dict)

    # Policy references (by name)
    policies: list[str] = Field(default_factory=_empty_str_list)

    # Plugin filtering
    plugins: PluginFilterSpec = Field(default_factory=PluginFilterSpec)

    # Memory binding
    memory: MemoryBindingSpec | None = None

    # Agent instances to create
    agents: list[WorkspaceAgentRef] = Field(default_factory=list)

    # Startup behavior
    startup: StartupSpec = Field(default_factory=StartupSpec)

    model_config = {"extra": "forbid"}


class WorkspaceSpec(BaseModel):
    """Top-level workspace specification — the runtime entrypoint."""

    api_version: str = Field("obscura/v1", alias="apiVersion")
    kind: Literal["Workspace"] = "Workspace"
    metadata: SpecMetadata
    spec: WorkspaceSpecBody

    model_config = {"populate_by_name": True, "extra": "forbid"}


# ---------------------------------------------------------------------------
# Union type for spec dispatch
# ---------------------------------------------------------------------------

AnySpec = TemplateSpec | AgentInstanceSpec | PolicySpec | WorkspaceSpec

SPEC_KIND_MAP: dict[str, type[AnySpec]] = {
    "Template": TemplateSpec,
    "Agent": AgentInstanceSpec,
    "Policy": PolicySpec,
    "Workspace": WorkspaceSpec,
}
