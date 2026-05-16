"""obscura.integrations.network_gateway.models — GET /v1/models handler.

Returns the list of Obscura backends as OpenAI-format model objects so any
OpenAI-compatible client can enumerate what this gateway exposes.
"""

from __future__ import annotations

import time

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class ModelObject(BaseModel):
    """OpenAI model list entry."""

    id: str
    object: str = "model"
    created: int
    owned_by: str = "obscura"


class ModelsResponse(BaseModel):
    """OpenAI /v1/models response envelope."""

    object: str = "list"
    data: list[ModelObject]


# Static catalog — one entry per Obscura backend plus the generic alias.
_BACKEND_DISPLAY: list[tuple[str, str]] = [
    ("obscura", "Obscura (default backend)"),
    ("obscura/claude", "Obscura → Claude"),
    ("obscura/copilot", "Obscura → GitHub Copilot"),
    ("obscura/codex", "Obscura → Codex"),
    ("obscura/localllm", "Obscura → Local LLM"),
]

_CREATED_TS = int(time.time())


@router.get("/v1/models", response_model=ModelsResponse, tags=["models"])
async def list_models() -> ModelsResponse:
    """List all Obscura backends in OpenAI model-list format."""
    data = [
        ModelObject(id=model_id, created=_CREATED_TS)
        for model_id, _ in _BACKEND_DISPLAY
    ]
    return ModelsResponse(data=data)


__all__ = ["router", "ModelsResponse", "ModelObject"]
