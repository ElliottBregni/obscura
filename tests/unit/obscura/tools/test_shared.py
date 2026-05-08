"""Unit tests for obscura.tools.system._shared — spec-provider registry."""

from __future__ import annotations

import pytest

from obscura.tools.system._shared import get_system_tool_specs, set_spec_provider

pytestmark = pytest.mark.unit


def test_get_system_tool_specs_no_provider_returns_empty() -> None:
    # Detach any provider registered by previous tests / full module load
    from obscura.tools.system import _shared

    original = _shared._provider
    _shared._provider = None
    try:
        result = get_system_tool_specs()
        assert result == []
    finally:
        _shared._provider = original


def test_set_spec_provider_registers_callable() -> None:
    from obscura.tools.system import _shared

    original = _shared._provider
    try:
        sentinel = object()
        set_spec_provider(lambda: [sentinel])  # type: ignore[list-item]
        result = get_system_tool_specs()
        assert result == [sentinel]
    finally:
        _shared._provider = original


def test_get_system_tool_specs_calls_provider_each_time() -> None:
    from obscura.tools.system import _shared

    call_count = 0

    def counting_provider() -> list:  # type: ignore[type-arg]
        nonlocal call_count
        call_count += 1
        return []

    original = _shared._provider
    try:
        set_spec_provider(counting_provider)
        get_system_tool_specs()
        get_system_tool_specs()
        assert call_count == 2
    finally:
        _shared._provider = original
