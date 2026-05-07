"""Smoke tests: every registered system tool spec must be structurally valid.

Parametrised over get_system_tool_specs() so new tools are automatically
included without touching this file.
"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import pytest

from obscura.core.types import ToolSpec
from obscura.tools.system import get_system_tool_specs

pytestmark = pytest.mark.unit

# Collect specs once at module load so parametrize IDs are stable.
_ALL_SPECS: list[ToolSpec] = get_system_tool_specs()


@pytest.mark.parametrize("spec", _ALL_SPECS, ids=lambda s: s.name)
def test_tool_spec_has_non_empty_name(spec: ToolSpec) -> None:
    assert isinstance(spec.name, str)
    assert spec.name.strip(), f"Tool spec has blank name: {spec!r}"


@pytest.mark.parametrize("spec", _ALL_SPECS, ids=lambda s: s.name)
def test_tool_spec_has_non_empty_description(spec: ToolSpec) -> None:
    assert isinstance(spec.description, str)
    assert spec.description.strip(), f"Tool '{spec.name}' has blank description"


@pytest.mark.parametrize("spec", _ALL_SPECS, ids=lambda s: s.name)
def test_tool_spec_handler_is_callable(spec: ToolSpec) -> None:
    assert callable(spec.handler), f"Tool '{spec.name}' handler is not callable"


@pytest.mark.parametrize("spec", _ALL_SPECS, ids=lambda s: s.name)
def test_tool_spec_parameters_schema_is_valid_object(spec: ToolSpec) -> None:
    params = spec.parameters
    assert isinstance(params, dict), (
        f"Tool '{spec.name}' parameters is not a dict: {type(params)}"
    )
    # If a top-level "type" key is present it must declare an object schema.
    # Some tools use a flat properties dict (no wrapper) which is also valid.
    if "type" in params:
        assert params["type"] == "object", (
            f"Tool '{spec.name}' parameters.type != 'object': {params.get('type')!r}"
        )


@pytest.mark.parametrize("spec", _ALL_SPECS, ids=lambda s: s.name)
def test_tool_spec_name_is_snake_case(spec: ToolSpec) -> None:
    """Tool names must be lowercase snake_case (no spaces, no uppercase)."""
    assert spec.name == spec.name.lower(), (
        f"Tool name '{spec.name}' contains uppercase letters"
    )
    assert " " not in spec.name, (
        f"Tool name '{spec.name}' contains spaces"
    )
