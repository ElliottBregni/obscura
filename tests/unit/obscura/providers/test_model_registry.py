"""Tests for provider model registry and caching."""

import pytest
from datetime import timedelta
from obscura.core.types import Backend
from obscura.providers.registry import ModelInfo, ProviderRegistry
from obscura.providers.model_cache import (
    ModelCache,
    list_provider_models,
    invalidate_cache,
    get_cache_age,
)


class MockProvider:
    """Mock provider implementing ProviderRegistry protocol."""

    def __init__(self, models: list[ModelInfo]):
        self._models = models
        self.call_count = 0

    async def list_models(self) -> list[ModelInfo]:
        self.call_count += 1
        return self._models

    def get_default_model(self) -> str:
        return "mock-model-1"

    def validate_model(self, model_id: str) -> bool:
        return model_id.startswith("mock-")


# ---------------------------------------------------------------------------
# ModelInfo tests
# ---------------------------------------------------------------------------


def test_model_info_creation():
    """Test ModelInfo dataclass creation."""
    model = ModelInfo(
        id="gpt-4",
        name="GPT-4",
        provider="openai",
        supports_tools=True,
        supports_vision=False,
    )
    assert model.id == "gpt-4"
    assert model.name == "GPT-4"
    assert model.provider == "openai"
    assert model.supports_tools is True
    assert model.supports_vision is False


def test_model_info_defaults():
    """Test ModelInfo with default values."""
    model = ModelInfo(
        id="test-model",
        name="Test Model",
        provider="test",
    )
    assert model.context_window is None
    assert model.max_output_tokens is None
    assert model.supports_tools is True  # Default
    assert model.supports_vision is False  # Default
    assert model.deprecated is False


# ---------------------------------------------------------------------------
# ModelCache tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_basic_functionality():
    """Test basic caching behavior."""
    mock_models = [
        ModelInfo(id="mock-1", name="Mock 1", provider="mock"),
        ModelInfo(id="mock-2", name="Mock 2", provider="mock"),
    ]
    mock_provider = MockProvider(mock_models)

    cache = ModelCache(ttl_seconds=3600)

    # First call should hit the provider
    models = await cache.get_models(Backend.OPENAI, mock_provider)
    assert len(models) == 2
    assert mock_provider.call_count == 1

    # Second call should use cache
    models = await cache.get_models(Backend.OPENAI, mock_provider)
    assert len(models) == 2
    assert mock_provider.call_count == 1  # Not incremented


@pytest.mark.asyncio
async def test_cache_expiration():
    """Test that cache expires after TTL."""
    mock_models = [ModelInfo(id="mock-1", name="Mock 1", provider="mock")]
    mock_provider = MockProvider(mock_models)

    # Very short TTL for testing
    cache = ModelCache(ttl_seconds=0)

    # First call
    await cache.get_models(Backend.OPENAI, mock_provider)
    assert mock_provider.call_count == 1

    # Second call after expiration
    await cache.get_models(Backend.OPENAI, mock_provider)
    assert mock_provider.call_count == 2  # Cache expired, called again


@pytest.mark.asyncio
async def test_cache_per_backend():
    """Test that cache is separate per backend."""
    mock_models_1 = [ModelInfo(id="mock-1", name="Mock 1", provider="mock1")]
    mock_models_2 = [ModelInfo(id="mock-2", name="Mock 2", provider="mock2")]

    mock_provider_1 = MockProvider(mock_models_1)
    mock_provider_2 = MockProvider(mock_models_2)

    cache = ModelCache(ttl_seconds=3600)

    # Cache for Backend.OPENAI
    models_1 = await cache.get_models(Backend.OPENAI, mock_provider_1)
    assert len(models_1) == 1
    assert models_1[0].id == "mock-1"

    # Cache for Backend.CLAUDE (different backend)
    models_2 = await cache.get_models(Backend.CLAUDE, mock_provider_2)
    assert len(models_2) == 1
    assert models_2[0].id == "mock-2"

    # Both should have been called once
    assert mock_provider_1.call_count == 1
    assert mock_provider_2.call_count == 1


@pytest.mark.asyncio
async def test_cache_fallback_on_error():
    """Test that cache falls back to stale data on provider error."""

    class FailingProvider:
        def __init__(self):
            self.should_fail = False

        async def list_models(self) -> list[ModelInfo]:
            if self.should_fail:
                raise Exception("Provider failed")
            return [ModelInfo(id="mock-1", name="Mock 1", provider="mock")]

    provider = FailingProvider()
    cache = ModelCache(ttl_seconds=0)  # Immediate expiration

    # First call succeeds
    models = await cache.get_models(Backend.OPENAI, provider)
    assert len(models) == 1

    # Make provider fail
    provider.should_fail = True

    # Second call should return stale cache despite error
    models = await cache.get_models(Backend.OPENAI, provider)
    assert len(models) == 1  # Stale data returned


# ---------------------------------------------------------------------------
# Global cache API tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_provider_models():
    """Test global list_provider_models API."""
    mock_models = [ModelInfo(id="mock-1", name="Mock 1", provider="mock")]
    mock_provider = MockProvider(mock_models)

    # Clear any existing cache first
    invalidate_cache(Backend.OPENAI)

    models = await list_provider_models(Backend.OPENAI, mock_provider)
    assert len(models) == 1
    assert models[0].id == "mock-1"


def test_invalidate_cache():
    """Test cache invalidation."""
    # This just ensures it doesn't error
    invalidate_cache(Backend.OPENAI)
    invalidate_cache(Backend.CLAUDE)


def test_get_cache_age():
    """Test getting cache age."""
    # Before any caching, should return None
    age = get_cache_age(Backend.OPENAI)
    # Age could be None (no cache) or a timedelta
    assert age is None or isinstance(age, timedelta)


# ---------------------------------------------------------------------------
# Provider-specific tests
# ---------------------------------------------------------------------------


def test_mock_provider_protocol():
    """Test that MockProvider implements ProviderRegistry correctly."""
    models = [ModelInfo(id="mock-1", name="Mock 1", provider="mock")]
    provider = MockProvider(models)

    # Should have all protocol methods
    assert hasattr(provider, "list_models")
    assert hasattr(provider, "get_default_model")
    assert hasattr(provider, "validate_model")

    # Test validate_model
    assert provider.validate_model("mock-123") is True
    assert provider.validate_model("other-123") is False

    # Test get_default_model
    assert provider.get_default_model() == "mock-model-1"


# ---------------------------------------------------------------------------
# Integration-style tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_integration():
    """Integration test simulating real usage pattern."""
    # Simulate a provider that returns different models over time
    class DynamicProvider:
        def __init__(self):
            self.version = 1

        async def list_models(self) -> list[ModelInfo]:
            return [
                ModelInfo(
                    id=f"model-v{self.version}",
                    name=f"Model v{self.version}",
                    provider="dynamic",
                )
            ]

    provider = DynamicProvider()
    cache = ModelCache(ttl_seconds=2)

    # First fetch
    models_v1 = await cache.get_models(Backend.OPENAI, provider)
    assert models_v1[0].id == "model-v1"

    # Update provider version (simulating new models)
    provider.version = 2

    # Immediate refetch should use cache (still v1)
    models_cached = await cache.get_models(Backend.OPENAI, provider)
    assert models_cached[0].id == "model-v1"

    # Wait for expiration
    import asyncio
    await asyncio.sleep(2.1)

    # Now should get new version
    models_v2 = await cache.get_models(Backend.OPENAI, provider)
    assert models_v2[0].id == "model-v2"


# ---------------------------------------------------------------------------
# Provider model catalog tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_model_catalog():
    """Test that Claude backend returns up-to-date model catalog."""
    from obscura.core.auth import AuthConfig
    from obscura.providers.claude import ClaudeBackend

    backend = ClaudeBackend(auth=AuthConfig())
    models = await backend.list_models()
    model_ids = [m.id for m in models]

    # Current models present
    assert "claude-opus-4-6" in model_ids
    assert "claude-sonnet-4-6" in model_ids
    assert "claude-haiku-4-5-20251001" in model_ids

    # Default is Sonnet 4.6
    assert backend.get_default_model() == "claude-sonnet-4-6"

    # All claude models pass validation
    for m in models:
        assert backend.validate_model(m.id) is True
    assert backend.validate_model("gpt-4o") is False


@pytest.mark.asyncio
async def test_openai_fallback_models():
    """Test that OpenAI backend fallback includes current models."""
    from obscura.core.auth import AuthConfig
    from obscura.providers.openai import OpenAIBackend

    backend = OpenAIBackend(auth=AuthConfig())
    # Client is not started, so list_models returns fallback
    models = await backend.list_models()
    model_ids = [m.id for m in models]

    assert "gpt-4o" in model_ids
    assert "gpt-4o-mini" in model_ids
    assert "o3-mini" in model_ids
    assert "o1" in model_ids


def test_openai_validates_o3():
    """Test that OpenAI validates o3-prefixed models."""
    from obscura.core.auth import AuthConfig
    from obscura.providers.openai import OpenAIBackend

    backend = OpenAIBackend(auth=AuthConfig())
    assert backend.validate_model("o3-mini") is True
    assert backend.validate_model("o3-mini-2025-01-31") is True
    assert backend.validate_model("gpt-4o") is True
    assert backend.validate_model("claude-sonnet-4-6") is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
