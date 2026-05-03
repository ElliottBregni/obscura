"""Routes: model discovery across providers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from obscura.auth.rbac import get_current_user
from obscura.core.auth import AuthConfig
from obscura.core.types import Backend
from obscura.providers.claude import ClaudeBackend
from obscura.providers.codex import CodexBackend
from obscura.providers.copilot import CopilotBackend
from obscura.providers.localllm import LocalLLMBackend
from obscura.providers.model_cache import (
    invalidate_cache,
    list_provider_models,
)
from obscura.providers.moonshot import MoonshotBackend
from obscura.providers.openai import OpenAIBackend

from obscura.auth.models import AuthenticatedUser
import logging

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from obscura.providers.registry import ModelInfo

router = APIRouter(prefix="/api/v1", tags=["models"])


def _get_backend_instance(backend: Backend) -> object:
    """Create a lightweight backend instance for model listing."""
    auth = AuthConfig()

    if backend == Backend.CLAUDE:
        return ClaudeBackend(auth=auth)
    if backend == Backend.OPENAI:
        return OpenAIBackend(auth=auth)
    if backend == Backend.COPILOT:
        return CopilotBackend(auth=auth)
    if backend == Backend.LOCALLLM:
        return LocalLLMBackend(auth=auth)
    if backend == Backend.CODEX:
        return CodexBackend(auth=auth)
    if backend == Backend.MOONSHOT:
        return MoonshotBackend(auth=auth)
    msg = f"Unknown backend: {backend}"
    raise ValueError(msg)


def _model_to_dict(model: ModelInfo) -> dict[str, object]:
    """Convert a ModelInfo to a JSON-serializable dict."""
    return {
        "id": model.id,
        "name": model.name,
        "provider": model.provider,
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
        "supports_tools": model.supports_tools,
        "supports_vision": model.supports_vision,
        "deprecated": model.deprecated,
    }


@router.get("/models")
async def list_models(
    provider: Annotated[
        str | None,
        Query(description="Filter by provider name"),
    ] = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    """List available models, optionally filtered by provider."""
    results: dict[str, list[dict[str, object]]] = {}

    if provider:
        try:
            backend = Backend(provider)
        except ValueError:
            logger.debug("suppressed exception in list_models", exc_info=True)
            return JSONResponse(
                status_code=400,
                content={"detail": f"Unknown provider: {provider}"},
            )
        instance = _get_backend_instance(backend)
        models = await list_provider_models(backend, instance)
        results[provider] = [_model_to_dict(m) for m in models]
    else:
        for backend in Backend:
            try:
                instance = _get_backend_instance(backend)
                models = await list_provider_models(backend, instance)
                results[backend.value] = [_model_to_dict(m) for m in models]
            except Exception:
                logger.debug("suppressed exception in list_models", exc_info=True)
                results[backend.value] = []

    return JSONResponse(content={"models": results})


@router.post("/models/refresh")
async def refresh_models(
    provider: Annotated[str | None, Query(description="Provider to refresh")] = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    """Invalidate the model cache and force a refresh."""
    if provider:
        try:
            backend = Backend(provider)
        except ValueError:
            logger.debug("suppressed exception in refresh_models", exc_info=True)
            return JSONResponse(
                status_code=400,
                content={"detail": f"Unknown provider: {provider}"},
            )
        invalidate_cache(backend)
    else:
        invalidate_cache()
    return JSONResponse(
        content={"status": "cache_invalidated", "provider": provider or "all"},
    )
