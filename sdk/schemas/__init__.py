"""
sdk.schemas -- Pydantic request/response models for the Obscura API.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SendRequest(BaseModel):
    """Request body for POST /api/v1/send."""
    backend: str = Field(default="copilot", description="Backend: 'copilot' or 'claude'")
    prompt: str = Field(..., min_length=1, description="User prompt text")
    model: str | None = Field(default=None, description="Raw model ID")
    model_alias: str | None = Field(default=None, description="copilot_models alias")
    system_prompt: str = Field(default="", description="System prompt")
    session_id: str | None = Field(default=None, description="Resume session by ID")


class SendResponse(BaseModel):
    """Response body for POST /api/v1/send."""
    text: str
    backend: str
    session_id: str | None = None


class StreamRequest(BaseModel):
    """Request body for POST /api/v1/stream."""
    backend: str = Field(default="copilot", description="Backend: 'copilot' or 'claude'")
    prompt: str = Field(..., min_length=1, description="User prompt text")
    model: str | None = Field(default=None, description="Raw model ID")
    model_alias: str | None = Field(default=None, description="copilot_models alias")
    system_prompt: str = Field(default="", description="System prompt")
    session_id: str | None = Field(default=None, description="Resume session by ID")


class SessionCreateRequest(BaseModel):
    """Request body for POST /api/v1/sessions."""
    backend: str = Field(default="copilot", description="Backend: 'copilot' or 'claude'")


class SessionResponse(BaseModel):
    """A single session reference."""
    session_id: str
    backend: str


class SyncRequest(BaseModel):
    """Request body for POST /api/v1/sync."""
    agent: str | None = Field(default=None, description="Specific agent to sync")
    repo: str | None = Field(default=None, description="Specific repo name or path")
    dry_run: bool = Field(default=False, description="Preview without changes")


class SyncResponse(BaseModel):
    """Response body for POST /api/v1/sync."""
    success: bool
    message: str


class HealthResponse(BaseModel):
    """Response body for GET /health and /ready."""
    status: str
