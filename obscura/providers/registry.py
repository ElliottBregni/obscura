"""Dynamic model registry for provider capabilities.

This module defines the protocol and data structures for providers to expose
their available models dynamically. Each provider backend implements the
ProviderRegistry protocol to enable model discovery without hard-coding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from abc import abstractmethod


def _empty_tuple() -> tuple[str, ...]:
    """Factory for empty tuple (pyright strict mode compatibility)."""
    return ()


@dataclass(frozen=True)
class ModelInfo:
    """Metadata about a model offered by a provider.
    
    This dataclass contains all relevant information about an LLM model,
    including capabilities, pricing, and technical specifications.
    
    Example:
        ModelInfo(
            id="gpt-4o",
            name="GPT-4 Optimized",
            provider="openai",
            context_window=128000,
            supports_tools=True,
            supports_vision=True,
        )
    
    Attributes:
        id: Unique model identifier (e.g., "gpt-4o", "claude-sonnet-4-5")
        name: Human-readable model name
        provider: Provider name ("openai", "claude", etc.)
        context_window: Maximum context size in tokens
        max_output_tokens: Maximum output size in tokens
        supports_tools: Whether the model supports function/tool calling
        supports_vision: Whether the model supports image inputs
        cost_per_1k_input: Input cost per 1000 tokens (USD)
        cost_per_1k_output: Output cost per 1000 tokens (USD)
        deprecated: Whether this model is deprecated
        aliases: Alternative names for this model
    """
    
    id: str
    name: str
    provider: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool = True
    supports_vision: bool = False
    cost_per_1k_input: float | None = None
    cost_per_1k_output: float | None = None
    deprecated: bool = False
    aliases: tuple[str, ...] = field(default_factory=_empty_tuple)


class ProviderRegistry(Protocol):
    """Protocol for providers to expose available models.
    
    All provider backends should implement these methods to enable
    dynamic model discovery. This allows Obscura to fetch available
    models at runtime without hard-coding model lists.
    
    Implementation Strategies:
    
    1. API-based (OpenAI, LocalLLM):
       - Fetch models from provider's API endpoint
       - Cache results to avoid excessive calls
       - Provide fallback list for offline/error cases
    
    2. SDK-based (Claude):
       - Return models from SDK metadata/documentation
       - Updated when SDK package updates
       - Will migrate to API-based when provider adds endpoint
    
    3. Hybrid (Copilot):
       - Use copilot_models package if available
       - Fall back to minimal list otherwise
    """
    
    @abstractmethod
    async def list_models(self) -> list[ModelInfo]:
        """Fetch available models from provider API or catalog.
        
        This method should return all models currently available from
        the provider. Implementations may:
        - Query a live API endpoint (OpenAI, LocalLLM)
        - Return a curated catalog (Claude, Copilot)
        - Use hybrid approaches
        
        Returns:
            List of ModelInfo objects describing available models.
            
        Raises:
            Exception: If model fetching fails and no fallback available.
            
        Note:
            Results are cached by ModelCache to avoid excessive calls.
            Implementations should handle errors gracefully and provide
            fallback lists when possible.
        """
        ...
    
    @abstractmethod
    def get_default_model(self) -> str | None:
        """Return the default model ID for this provider.
        
        This is the model used when the user doesn't specify a model_id.
        
        Returns:
            Model ID string (e.g., "gpt-4o", "claude-sonnet-4-5"),
            or None if the provider auto-selects (e.g., Copilot).
            
        Example:
            OpenAI: "gpt-4o"
            Claude: "claude-sonnet-4-5-20250929"
            Copilot: None (auto-select)
        """
        ...
    
    @abstractmethod
    def validate_model(self, model_id: str) -> bool:
        """Check if a model ID is valid for this provider.
        
        This is a quick client-side validation check. The provider API
        may still reject the model if it's not actually available.
        
        Args:
            model_id: Model identifier to validate.
            
        Returns:
            True if the model ID format is valid for this provider,
            False otherwise.
            
        Note:
            This is a best-effort check. Implementations may:
            - Check against known patterns (e.g., starts with "gpt-")
            - Always return True and let the API validate
            - Check against the cached model list
            
        Example:
            OpenAI: Check if starts with "gpt-", "o1-", etc.
            Claude: Check if starts with "claude-"
            LocalLLM: Always return True (server validates)
        """
        ...
