"""Pydantic models for agent spawn/bulk spawn endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from obscura.schemas.templates import APERProfileSchema, A2ARemoteToolsSpecSchema


def _default_dict_list() -> list[dict[str, Any]]:
    return []


def _default_str_list() -> list[str]:
    return []


def _default_spawn_list() -> list[AgentSpawnRequest]:
    return []


class MCPRuntimeSchema(BaseModel):
    """Runtime MCP config accepted by spawn endpoints."""

    enabled: bool = False
    servers: list[dict[str, Any]] = Field(default_factory=_default_dict_list)
    config_path: str = Field(default=".obscura/mcp")
    server_names: list[str] = Field(default_factory=list)
    primary_server_name: str = Field(default="github")
    auto_discover: bool = Field(default=True)
    resolve_env: bool = Field(default=True)


class AgentBuilderSpawnSchema(BaseModel):
    """Builder-style spawn payload for API parity with AgentBuilder."""

    name: str = Field(default="unnamed")
    provider: str = Field(default="copilot", description="Backend: copilot, claude, openai, etc.")
    model_id: str | None = Field(default=None, description="Specific model ID (optional)")
    
    # Deprecated field for backward compatibility
    model: str | None = Field(
        default=None,
        description="DEPRECATED: Use 'provider' instead.",
        deprecated=True,
    )
    system_prompt: str = Field(default="")
    memory_namespace: str = Field(default="default")
    max_iterations: int = Field(default=10, ge=1)
    timeout_seconds: float = Field(default=300.0, gt=0)
    enable_system_tools: bool = Field(default=True)
    parent_agent_id: str | None = Field(default=None)
    tags: list[str] = Field(default_factory=_default_str_list)

    # APER-adjacent config (stored for parity; execution mode still selected at run-time)
    aper_profile: APERProfileSchema | None = Field(default=None)

    # Builder integrations
    skills: list[dict[str, Any]] = Field(default_factory=_default_dict_list)
    mcp: MCPRuntimeSchema | None = Field(default=None)
    mcp_auto_discover: bool = Field(default=False)
    mcp_config_path: str = Field(default="config/mcp-config.json")
    mcp_server_names: list[str] = Field(default_factory=_default_str_list)
    mcp_primary_server_name: str = Field(default="github")
    mcp_resolve_env: bool = Field(default=True)
    @model_validator(mode='after')
    def migrate_model_field(self):
        """Migrate deprecated 'model' field to 'provider'."""
        if self.model is not None and self.provider == "copilot":
            import warnings
            warnings.warn(
                "The 'model' field is deprecated. Use 'provider' instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            self.provider = self.model
        return self

    
    a2a_remote_tools: A2ARemoteToolsSpecSchema | dict[str, Any] | None = Field(
        default=None
    )


class AgentSpawnRequest(BaseModel):
    """POST /api/v1/agents body."""

    name: str = Field(default="unnamed")
    provider: str = Field(default="copilot", description="Backend: copilot, claude, openai, etc.")
    model_id: str | None = Field(default=None, description="Specific model ID (optional)")
    
    # Deprecated field for backward compatibility
    model: str | None = Field(
        default=None,
        description="DEPRECATED: Use 'provider' instead.",
        deprecated=True,
    )
    system_prompt: str = Field(default="")
    memory_namespace: str = Field(default="default")
    max_iterations: int = Field(default=10, ge=1)
    timeout_seconds: float = Field(default=300.0, gt=0)
    enable_system_tools: bool = Field(default=True)
    parent_agent_id: str | None = Field(default=None)
    tags: list[str] = Field(default_factory=_default_str_list)
    mcp: MCPRuntimeSchema | dict[str, Any] | None = Field(default=None)
    @model_validator(mode='after')
    def migrate_model_field(self):
        """Migrate deprecated 'model' field to 'provider'."""
        if self.model is not None and self.provider == "copilot":
            import warnings
            warnings.warn(
                "The 'model' field is deprecated. Use 'provider' instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            self.provider = self.model
        return self

    
    a2a_remote_tools: A2ARemoteToolsSpecSchema | dict[str, Any] | None = Field(
        default=None
    )

    # New typed builder-compatible envelope
    builder: AgentBuilderSpawnSchema | dict[str, Any] | None = Field(default=None)


class AgentBulkSpawnRequest(BaseModel):
    """POST /api/v1/agents/bulk body."""

    agents: list[AgentSpawnRequest] = Field(default_factory=_default_spawn_list)
