# Provider/Model Separation - Implementation Plan

## Context

The Obscura codebase currently conflates "provider" (backend/LLM service) with "model" (specific model ID). The `model` field is used to mean both provider names (`"claude"`, `"openai"`) and actual model IDs (`"claude-sonnet-4-5-20250929"`, `"gpt-4o"`). This creates confusion and limits user control over which specific model they use.

**Goal**: Clean separation with dynamic model discovery (NO hard-coded lists).

---

## Phase 1: Provider Model Registry (APPROVED FOR IMPLEMENTATION)

### Files to Create

1. **`obscura/providers/registry.py`** - Protocol + ModelInfo dataclass
2. **`obscura/providers/model_cache.py`** - Caching layer with TTL

### Files to Modify

Update each provider to implement `ProviderRegistry` protocol:
- `obscura/providers/openai.py` - API-based discovery
- `obscura/providers/claude.py` - SDK catalog
- `obscura/providers/copilot.py` - Hybrid (copilot_models)
- `obscura/providers/localllm.py` - Runtime server discovery
- `obscura/providers/codex.py` - Minimal implementation
- `obscura/providers/moonshot.py` - Inherit from OpenAI

### Implementation Order

1. Create `registry.py` with `ModelInfo` + `ProviderRegistry` protocol
2. Create `model_cache.py` with caching + public API
3. Update OpenAI backend (simplest - has real API)
4. Update Claude backend (SDK catalog)
5. Update Copilot backend (copilot_models integration)
6. Update LocalLLM backend (runtime discovery)
7. Update Codex + Moonshot (minimal)

---

## Testing Strategy

After Phase 1:
```python
# Test dynamic discovery
from obscura import ObscuraClient
from obscura.core.types import Backend
from obscura.providers.model_cache import list_provider_models

async with ObscuraClient("openai") as client:
    models = await list_provider_models(Backend.OPENAI, client._backend)
    assert len(models) > 0
    assert all(m.provider == "openai" for m in models)
```

---

## Success Criteria for Phase 1

✅ Registry protocol defined  
✅ Model cache implemented with TTL  
✅ All providers implement `list_models()`  
✅ No hard-coded business logic (only SDK catalogs)  
✅ Caching works (1-hour TTL)  
✅ Tests pass  

---

## Next Phases (After Phase 1)

- Phase 2: Data model changes (provider/model_id fields)
- Phase 3: API schema updates
- Phase 4: Client & CLI updates
- Phase 5: Testing & migration

USER APPROVED: Ready to implement Phase 1
