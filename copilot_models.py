"""
copilot_models — Copilot model aliases and safety guards.

Central configuration for all Copilot model usage. Scripts MUST use aliases
from this module instead of raw model IDs. This prevents accidental premium
model usage in automation and makes cost behavior predictable.

Principles:
    - Explicit over implicit: no default model, every call goes through an alias
    - Alias = intent: the name communicates what the model is allowed to do
    - Fail closed: unknown aliases or premium models in automation → hard error
    - Automation-first: cheaper models are the default for unattended workflows
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelConfig:
    """A resolved model configuration."""
    alias: str
    model_id: str
    category: str  # "automation" | "interactive" | "premium"
    max_requests_per_run: int | None  # None = unlimited


# Category constants
AUTOMATION = "automation"
INTERACTIVE = "interactive"
PREMIUM = "premium"

# Model alias → model ID mapping.
# This is the ONE place where model IDs live.
_ALIAS_REGISTRY: dict[str, ModelConfig] = {
    # --- Automation / Unattended ---
    "copilot_automation_safe": ModelConfig(
        alias="copilot_automation_safe",
        model_id="gpt-5-mini",
        category=AUTOMATION,
        max_requests_per_run=500,
    ),
    "copilot_batch_diagrammer": ModelConfig(
        alias="copilot_batch_diagrammer",
        model_id="gpt-5-mini",
        category=AUTOMATION,
        max_requests_per_run=200,
    ),

    # --- Interactive / Exploration ---
    "copilot_interactive_reasoning": ModelConfig(
        alias="copilot_interactive_reasoning",
        model_id="gpt-5",
        category=INTERACTIVE,
        max_requests_per_run=50,
    ),

    # --- Premium / Restricted ---
    "copilot_premium_manual_only": ModelConfig(
        alias="copilot_premium_manual_only",
        model_id="o3",
        category=PREMIUM,
        max_requests_per_run=10,
    ),
}

# Models that are considered premium (expensive). If a model ID contains
# any of these substrings, it is flagged as premium.
_PREMIUM_MODEL_PATTERNS: set[str] = {"o3", "o1", "gpt-5-turbo", "gpt-5"}

# Models explicitly safe for automation.
_AUTOMATION_SAFE_MODELS: set[str] = {"gpt-5-mini", "gpt-4o-mini", "gpt-4o"}


# ---------------------------------------------------------------------------
# Environment override
# ---------------------------------------------------------------------------

def _apply_env_override(alias: str, config: ModelConfig) -> ModelConfig:
    """Allow env-var override of model ID, but only within the same category."""
    env_key = f"COPILOT_MODEL_{alias.upper()}"
    override = os.environ.get(env_key)
    if override is None:
        return config

    if config.category == AUTOMATION and override not in _AUTOMATION_SAFE_MODELS:
        print(
            f"[copilot_models] BLOCKED: env {env_key}={override} "
            f"is not automation-safe. Keeping {config.model_id}.",
            file=sys.stderr,
        )
        return config

    return ModelConfig(
        alias=config.alias,
        model_id=override,
        category=config.category,
        max_requests_per_run=config.max_requests_per_run,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve(alias: str) -> ModelConfig:
    """Resolve an alias to a ModelConfig. Raises ValueError for unknown aliases."""
    config = _ALIAS_REGISTRY.get(alias)
    if config is None:
        known = ", ".join(sorted(_ALIAS_REGISTRY))
        raise ValueError(
            f"Unknown copilot alias: {alias!r}. "
            f"Known aliases: {known}. "
            f"Raw model IDs are not allowed — use an alias."
        )
    return _apply_env_override(alias, config)


def get_model_id(alias: str) -> str:
    """Shorthand: resolve alias and return the model ID string."""
    return resolve(alias).model_id


def require_automation_safe(alias: str) -> ModelConfig:
    """Resolve alias and assert it is safe for unattended automation.

    Raises ValueError if the alias points to an interactive or premium model.
    """
    config = resolve(alias)
    if config.category != AUTOMATION:
        raise ValueError(
            f"Alias {alias!r} (category={config.category}) is NOT safe for "
            f"automation. Use an automation alias like: "
            + ", ".join(
                a for a, c in _ALIAS_REGISTRY.items() if c.category == AUTOMATION
            )
        )
    if config.model_id not in _AUTOMATION_SAFE_MODELS:
        raise ValueError(
            f"Alias {alias!r} resolved to model {config.model_id!r}, "
            f"which is not in the automation-safe list: {_AUTOMATION_SAFE_MODELS}"
        )
    return config


def guard_automation(alias: str) -> str:
    """Resolve, validate for automation, and return the model ID.

    This is the primary entry point for scripts and batch jobs.
    """
    config = require_automation_safe(alias)
    return config.model_id


def list_aliases() -> dict[str, ModelConfig]:
    """Return a copy of the alias registry for inspection."""
    return dict(_ALIAS_REGISTRY)
