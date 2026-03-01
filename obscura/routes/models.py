"""Routes: model discovery across providers."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from obscura.auth.models import AuthenticatedUser
from obscura.auth.rbac import get_current_user
from obscura.core.types import Backend
from obscura.providers.model_cache import (
    list_provider_models,
    invalidate_cache,
)
from obscura.providers.registry import ModelInfo

router = APIRouter(prefix="/api/v1", tags=["models"])


def _get_backend_instance(backend: Backend) -> object:
    """Create a lightweight backend instance for model listing."""
    from obscura.core.auth import AuthConfig

    auth = AuthConfig()

    if backend == Backend.CLAUDE:
        from obscura.providers.claude import ClaudeBackend
        return ClaudeBackend(auth=auth)
    if backend == Backend.OPENAI:
        from obscura.providers.openai import OpenAIBackend
        return OpenAIBackend(auth=auth)
    if backend == Backend.COPILOT:
        from obscura.providers.copilot import CopilotBackend
        return CopilotBackend(auth=auth)
    if backend == Backend.LOCALLLM:
        from obscura.providers.localllm import LocalLLMBackend
        return LocalLLMBackend(auth=auth)
    if backend == Backend.CODEX:
        from obscura.providers.codex import CodexBackend
        return CodexBackend(auth=auth)
    if backend == Backend.MOONSHOT:
        from obscura.providers.moonshot import MoonshotBackend
        return MoonshotBackend(auth=auth)
    raise ValueError(f"Unknown backend: {backend}")


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
    provider: str | None = Query(None, description="Filter by provider name"),
    user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    """List available models, optionally filtered by provider."""
    results: dict[str, list[dict[str, object]]] = {}

    if provider:
        try:
            backend = Backend(provider)
        except ValueError:
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
                results[backend.value] = []

    return JSONResponse(content={"models": results})


@router.post("/models/refresh")
async def refresh_models(
    provider: str | None = Query(None, description="Provider to refresh"),
    user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    """Invalidate the model cache and force a refresh."""
    if provider:
        try:
            backend = Backend(provider)
        except ValueError:
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
