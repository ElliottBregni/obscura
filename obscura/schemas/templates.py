"""Pydantic models for APER agent template CRUD endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Sub-schemas
# ---------------------------------------------------------------------------


class APERProfileSchema(BaseModel):
    """APER phase prompt templates."""

    analyze_template: str = Field(
        default="Analyze the user goal and extract constraints.",
        description="Prompt template for the Analyze phase",
    )
    plan_template: str = Field(
        default="Create a step-by-step plan to solve the goal.",
        description="Prompt template for the Plan phase",
    )
    execute_template: str = Field(
        default=(
            "Goal:\n{goal}\n\nAnalysis:\n{analysis}\n\nPlan:\n{plan}\n\n"
            "Execute using tools where useful and return concise output."
        ),
        description="Prompt template for the Execute phase (supports {goal}, {analysis}, {plan})",
    )
    respond_template: str = Field(
        default="Return a final concise answer based on execution output.",
        description="Prompt template for the Respond phase",
    )
    max_turns: int = Field(default=8, ge=1, le=100, description="Max LLM turns during execute")


class SkillSpecSchema(BaseModel):
    """An inline skill injected into the system prompt."""

    name: str = Field(..., min_length=1, description="Skill identifier")
    content: str = Field(..., min_length=1, description="Skill content (markdown or text)")
    source: str = Field(default="inline", description="Origin: 'inline' or file path")


class MCPServerSpecSchema(BaseModel):
    """MCP server configuration."""

    name: str = Field(..., min_length=1)
    transport: Literal["stdio", "sse"]
    command: str = Field(default="", description="Command for stdio transport")
    args: list[str] = Field(default_factory=list, description="Args for stdio transport")
    url: str = Field(default="", description="URL for sse transport")
    env: dict[str, str] = Field(default_factory=dict)


class A2ARemoteToolsSpecSchema(BaseModel):
    """A2A remote tools configuration."""

    enabled: bool = True
    urls: list[str] = Field(default_factory=list, description="Remote agent URLs")
    auth_token: str | None = Field(default=None, description="Optional auth token")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class TemplateCreateRequest(BaseModel):
    """POST /api/v1/agent-templates request body."""

    # Basic fields (existing)
    name: str = Field(default="unnamed-template", min_length=1)
    provider: str = Field(default="claude", description="Backend provider")
    model_id: str | None = Field(default=None, description="Specific model ID (optional)")
    system_prompt: str = Field(default="")
    timeout_seconds: float = Field(default=300.0, gt=0)
    max_iterations: int = Field(default=10, ge=1)
    memory_namespace: str = Field(default="default")
    tags: list[str] = Field(default_factory=list)

    # Extended fields (new)
    enable_system_tools: bool = Field(default=True)
    lifecycle_logs_enabled: bool = Field(default=True)
    parent_agent_id: str | None = Field(default=None)
    aper_profile: APERProfileSchema | None = Field(
        default=None, description="APER phase config; null = no APER"
    )
    skills: list[SkillSpecSchema] = Field(default_factory=list)
    mcp_servers: list[MCPServerSpecSchema] = Field(default_factory=list)
    mcp_auto_discover: bool = Field(default=False)
    mcp_config_path: str = Field(default="config/mcp-config.json")
    mcp_server_names: list[str] = Field(default_factory=list)
    mcp_primary_server_name: str = Field(default="github")
    mcp_resolve_env: bool = Field(default=True)
    a2a_remote_tools: A2ARemoteToolsSpecSchema | None = Field(default=None)

    # Persistence
    persist: bool = Field(
        default=False, description="Save to SQLite for server-restart durability"
    )


class TemplateUpdateRequest(BaseModel):
    """PUT /api/v1/agent-templates/{template_id} request body.

    Partial update — only non-None fields are merged.
    """

    name: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    max_iterations: int | None = Field(default=None, ge=1)
    memory_namespace: str | None = None
    tags: list[str] | None = None
    enable_system_tools: bool | None = None
    lifecycle_logs_enabled: bool | None = None
    parent_agent_id: str | None = None
    aper_profile: APERProfileSchema | None = None
    skills: list[SkillSpecSchema] | None = None
    mcp_servers: list[MCPServerSpecSchema] | None = None
    mcp_auto_discover: bool | None = None
    mcp_config_path: str | None = None
    mcp_server_names: list[str] | None = None
    mcp_primary_server_name: str | None = None
    mcp_resolve_env: bool | None = None
    a2a_remote_tools: A2ARemoteToolsSpecSchema | None = None
    persist: bool | None = None


class SpawnFromTemplateRequest(BaseModel):
    """POST /api/v1/agents/from-template request body."""

    template_id: str = Field(..., min_length=1)
    name: str | None = Field(default=None, description="Override template name")
    prompt: str = Field(default="", description="Initial prompt for APER mode")
    mode: Literal["run", "loop", "stream", "aper"] = Field(
        default="loop", description="Run mode; 'aper' uses the template's APERProfile"
    )


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class TemplateResponse(BaseModel):
    """Full template representation returned by all template endpoints."""

    template_id: str
    name: str
    model: str
    system_prompt: str
    timeout_seconds: float
    max_iterations: int
    memory_namespace: str
    tags: list[str]
    enable_system_tools: bool
    lifecycle_logs_enabled: bool
    parent_agent_id: str | None
    aper_profile: APERProfileSchema | None
    skills: list[SkillSpecSchema]
    mcp_servers: list[MCPServerSpecSchema]
    mcp_auto_discover: bool
    mcp_config_path: str
    mcp_server_names: list[str]
    mcp_primary_server_name: str
    mcp_resolve_env: bool
    a2a_remote_tools: A2ARemoteToolsSpecSchema | None
    persist: bool
    created_by: str
    created_at: str
    updated_at: str | None = None


class TemplateListResponse(BaseModel):
    """GET /api/v1/agent-templates response body."""

    templates: list[TemplateResponse]
    count: int
