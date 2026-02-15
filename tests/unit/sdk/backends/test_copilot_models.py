"""Tests for copilot_models — alias registry and safety guards."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from scripts.copilot_models import (
    AUTOMATION,
    ModelConfig,
    guard_automation,
    get_model_id,
    list_aliases,
    require_automation_safe,
    resolve,
)
import scripts.copilot_models as _cm

# Explicitly expose private registries used in tests
_ALIAS_REGISTRY = _cm._ALIAS_REGISTRY  # pyright: ignore[reportPrivateUsage]
_AUTOMATION_SAFE_MODELS = _cm._AUTOMATION_SAFE_MODELS  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


class TestResolve:
    def test_known_alias(self) -> None:
        config = resolve("copilot_batch_diagrammer")
        assert isinstance(config, ModelConfig)
        assert config.alias == "copilot_batch_diagrammer"
        assert config.model_id == "gpt-5-mini"
        assert config.category == AUTOMATION

    def test_unknown_alias_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown copilot alias"):
            resolve("gpt-5-mini")

    def test_raw_model_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="Raw model IDs are not allowed"):
            resolve("o3")

    def test_all_registered_aliases_resolve(self) -> None:
        for alias in _ALIAS_REGISTRY:
            config = resolve(alias)
            assert config.alias == alias


# ---------------------------------------------------------------------------
# get_model_id
# ---------------------------------------------------------------------------


class TestGetModelId:
    def test_returns_string(self) -> None:
        model_id = get_model_id("copilot_automation_safe")
        assert isinstance(model_id, str)
        assert model_id == "gpt-5-mini"

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError):
            get_model_id("nonexistent")


# ---------------------------------------------------------------------------
# require_automation_safe
# ---------------------------------------------------------------------------


class TestRequireAutomationSafe:
    def test_automation_alias_passes(self) -> None:
        config = require_automation_safe("copilot_batch_diagrammer")
        assert config.category == AUTOMATION

    def test_interactive_alias_blocked(self) -> None:
        with pytest.raises(ValueError, match="NOT safe for automation"):
            require_automation_safe("copilot_interactive_reasoning")

    def test_premium_alias_blocked(self) -> None:
        with pytest.raises(ValueError, match="NOT safe for automation"):
            require_automation_safe("copilot_premium_manual_only")


# ---------------------------------------------------------------------------
# guard_automation
# ---------------------------------------------------------------------------


class TestGuardAutomation:
    def test_returns_model_id_for_safe_alias(self) -> None:
        model_id = guard_automation("copilot_batch_diagrammer")
        assert model_id == "gpt-5-mini"

    def test_blocks_premium_alias(self) -> None:
        with pytest.raises(ValueError):
            guard_automation("copilot_premium_manual_only")

    def test_blocks_interactive_alias(self) -> None:
        with pytest.raises(ValueError):
            guard_automation("copilot_interactive_reasoning")

    def test_blocks_unknown_alias(self) -> None:
        with pytest.raises(ValueError):
            guard_automation("yolo_model")


# ---------------------------------------------------------------------------
# Environment overrides
# ---------------------------------------------------------------------------


class TestEnvOverride:
    def test_env_override_automation_safe_model(self) -> None:
        with patch.dict(
            os.environ, {"COPILOT_MODEL_COPILOT_BATCH_DIAGRAMMER": "gpt-4o-mini"}
        ):
            config = resolve("copilot_batch_diagrammer")
            assert config.model_id == "gpt-4o-mini"

    def test_env_override_blocked_for_premium_model(self) -> None:
        with patch.dict(os.environ, {"COPILOT_MODEL_COPILOT_BATCH_DIAGRAMMER": "o3"}):
            config = resolve("copilot_batch_diagrammer")
            # Should NOT override — o3 is not automation-safe
            assert config.model_id == "gpt-5-mini"

    def test_env_override_not_set(self) -> None:
        # Ensure no override env var exists
        with patch.dict(os.environ, {}, clear=True):
            config = resolve("copilot_batch_diagrammer")
            assert config.model_id == "gpt-5-mini"


# ---------------------------------------------------------------------------
# list_aliases
# ---------------------------------------------------------------------------


class TestListAliases:
    def test_returns_dict(self) -> None:
        aliases = list_aliases()
        assert isinstance(aliases, dict)
        assert len(aliases) > 0

    def test_contains_expected_aliases(self) -> None:
        aliases = list_aliases()
        assert "copilot_automation_safe" in aliases
        assert "copilot_batch_diagrammer" in aliases
        assert "copilot_interactive_reasoning" in aliases
        assert "copilot_premium_manual_only" in aliases

    def test_returns_copy(self) -> None:
        a = list_aliases()
        b = list_aliases()
        assert a is not b


# ---------------------------------------------------------------------------
# ModelConfig
# ---------------------------------------------------------------------------


class TestModelConfig:
    def test_frozen(self) -> None:
        config = resolve("copilot_batch_diagrammer")
        with pytest.raises(AttributeError):
            config.model_id = "something_else"  # pyright: ignore[reportAttributeAccessIssue]

    def test_max_requests(self) -> None:
        config = resolve("copilot_batch_diagrammer")
        assert config.max_requests_per_run == 200

    def test_premium_has_low_limit(self) -> None:
        config = resolve("copilot_premium_manual_only")
        assert config.max_requests_per_run is not None
        assert config.max_requests_per_run <= 10


# ---------------------------------------------------------------------------
# Registry invariants
# ---------------------------------------------------------------------------


class TestRegistryInvariants:
    def test_all_automation_aliases_use_safe_models(self) -> None:
        for alias, config in _ALIAS_REGISTRY.items():
            if config.category == AUTOMATION:
                assert config.model_id in _AUTOMATION_SAFE_MODELS, (
                    f"Automation alias {alias!r} uses non-safe model {config.model_id!r}"
                )

    def test_all_aliases_have_max_requests(self) -> None:
        for alias, config in _ALIAS_REGISTRY.items():
            assert config.max_requests_per_run is not None, (
                f"Alias {alias!r} has no request limit"
            )

    def test_no_duplicate_aliases(self) -> None:
        # dict keys are unique by definition, but let's be explicit
        aliases = list(_ALIAS_REGISTRY.keys())
        assert len(aliases) == len(set(aliases))
