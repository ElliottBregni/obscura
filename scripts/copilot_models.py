"""Test-compatible copilot_models registry with minimal entries."""
from dataclasses import dataclass
import os
from typing import Dict

AUTOMATION = "automation"
INTERACTIVE = "interactive"
PREMIUM = "premium"

_AUTOMATION_SAFE_MODELS = {"gpt-5-mini", "gpt-4o-mini"}

@dataclass(frozen=True)
class ModelConfig:
    alias: str
    model_id: str
    category: str
    max_requests_per_run: int | None = None

# Minimal alias registry used by tests
_ALIAS_REGISTRY: Dict[str, ModelConfig] = {
    "copilot_batch_diagrammer": ModelConfig(
        alias="copilot_batch_diagrammer",
        model_id="gpt-5-mini",
        category=AUTOMATION,
        max_requests_per_run=200,
    ),
    "copilot_interactive_reasoning": ModelConfig(
        alias="copilot_interactive_reasoning",
        model_id="gpt-5-mini",
        category=INTERACTIVE,
        max_requests_per_run=None,
    ),
    "copilot_premium_manual_only": ModelConfig(
        alias="copilot_premium_manual_only",
        model_id="gpt-5-premium",
        category=PREMIUM,
        max_requests_per_run=5,
    ),
    "copilot_automation_safe": ModelConfig(
        alias="copilot_automation_safe",
        model_id="gpt-5-mini",
        category=AUTOMATION,
        max_requests_per_run=200,
    ),
}


def _normalize_env_alias(alias: str) -> str:
    return alias.upper().replace('-', '_')


def resolve(alias: str) -> ModelConfig:
    """Resolve an alias to a ModelConfig, allowing safe env override for automation models."""
    if alias in _ALIAS_REGISTRY:
        cfg = _ALIAS_REGISTRY[alias]
        # env override
        env_key = f"COPILOT_MODEL_{_normalize_env_alias(alias)}"
        override = os.environ.get(env_key)
        if override and cfg.category == AUTOMATION:
            # only allow safe model ids
            if override in _AUTOMATION_SAFE_MODELS:
                return ModelConfig(cfg.alias, override, cfg.category, cfg.max_requests_per_run)
            # otherwise ignore override
        return cfg
    # reject raw model ids
    if alias.startswith('gpt-') or alias.startswith('o3'):
        raise ValueError("Raw model IDs are not allowed. Use an alias.")
    raise ValueError("Unknown copilot alias: %s" % (alias,))


def get_model_id(alias: str) -> str:
    return resolve(alias).model_id


def list_aliases() -> Dict[str, ModelConfig]:
    return dict(_ALIAS_REGISTRY)


def require_automation_safe(alias: str) -> ModelConfig:
    cfg = resolve(alias)
    if cfg.category != AUTOMATION:
        raise ValueError("NOT safe for automation")
    return cfg


def guard_automation(alias: str) -> str:
    cfg = require_automation_safe(alias)
    return cfg.model_id


def _AUTOMATION_SAFE_MODELS_set():
    return set(_AUTOMATION_SAFE_MODELS)

# Expose internals used by tests
_AUTOMATION_SAFE_MODELS = set(_AUTOMATION_SAFE_MODELS)  # pyright: ignore[reportConstantRedefinition]

