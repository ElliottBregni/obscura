# Patch for OpenAI backend - add these imports and methods

# Add to imports section (after line 47):
from obscura.providers.registry import ModelInfo as RegistryModelInfo, ProviderRegistry

# Replace the existing list_models method (line 647-651) with:

async def list_models(self) -> list[RegistryModelInfo]:
    """Fetch available models from OpenAI API.
    
    Implements ProviderRegistry protocol.
    Dynamic discovery - fetches from live API.
    """
    self._ensure_client()
    
    try:
        # Call OpenAI's models.list() endpoint
        models_response = await self._client.models.list()
        
        model_list = []
        for model in models_response.data:
            # Filter to chat models only (exclude embeddings, etc.)
            if not model.id.startswith(('gpt-', 'o1-', 'chatgpt-')):
                continue
            
            # Parse context window from model ID if available
            context_window = None
            if 'gpt-4' in model.id:
                context_window = 128000
            elif 'gpt-3.5' in model.id:
                context_window = 16385
            
            model_list.append(RegistryModelInfo(
                id=model.id,
                name=self._format_model_name(model.id),
                provider="openai",
                context_window=context_window,
                supports_tools=True,
                supports_vision='vision' in model.id or 'gpt-4o' in model.id,
            ))
        
        return model_list
        
    except Exception as e:
        # Fallback to known models if API fails
        import logging
        logging.warning(f"Failed to fetch OpenAI models: {e}. Using fallback list.")
        return self._get_fallback_models()

def _format_model_name(self, model_id: str) -> str:
    """Convert model ID to human-readable name."""
    name_map = {
        'gpt-4o': 'GPT-4 Optimized',
        'gpt-4o-mini': 'GPT-4 Optimized Mini',
        'gpt-4-turbo': 'GPT-4 Turbo',
        'gpt-4': 'GPT-4',
        'gpt-3.5-turbo': 'GPT-3.5 Turbo',
    }
    return name_map.get(model_id, model_id.upper())

def _get_fallback_models(self) -> list[RegistryModelInfo]:
    """Minimal fallback if API unavailable."""
    return [
        RegistryModelInfo(
            id="gpt-4o",
            name="GPT-4 Optimized",
            provider="openai",
            context_window=128000,
            supports_tools=True,
            supports_vision=True,
        ),
        RegistryModelInfo(
            id="gpt-4o-mini",
            name="GPT-4 Optimized Mini",
            provider="openai",
            context_window=128000,
            supports_tools=True,
            supports_vision=True,
        ),
    ]

def get_default_model(self) -> str:
    """Return default model for OpenAI."""
    return "gpt-4o"

def validate_model(self, model_id: str) -> bool:
    """Validate model ID format."""
    valid_prefixes = ('gpt-', 'o1-', 'chatgpt-', 'text-')
    return any(model_id.startswith(prefix) for prefix_in valid_prefixes)
