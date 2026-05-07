"""Unit tests for obscura.tools.result — ToolResult re-export boundary."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_tool_result_importable() -> None:
    from obscura.tools.result import ToolResult

    assert ToolResult is not None


def test_tool_result_builder_importable() -> None:
    from obscura.tools.result import ToolResultBuilder

    assert ToolResultBuilder is not None


def test_tool_result_all_exports() -> None:
    import obscura.tools.result as _mod

    assert "ToolResult" in _mod.__all__
    assert "ToolResultBuilder" in _mod.__all__
