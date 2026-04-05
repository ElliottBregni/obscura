"""Tests for obscura.plugins.result_formatter — configurable tool output formatting."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from obscura.plugins.result_formatter import OutputLevel, format_tool_result


@dataclass(frozen=True)
class FakeToolSpec:
    """Minimal ToolSpec stand-in for formatter tests."""

    name: str = "test_tool"
    output_schema: dict[str, Any] = field(default_factory=dict)


SHELL_SCHEMA: dict[str, Any] = {
    "x-output-levels": {
        "minimal": ["ok"],
        "standard": ["ok", "stdout", "exit_code"],
        "full": ["ok", "stdout", "stderr", "exit_code", "command"],
    },
    "x-default-level": "standard",
}


FULL_RESULT: dict[str, Any] = {
    "ok": True,
    "exit_code": 0,
    "stdout": "hello world\n",
    "stderr": "",
    "command": "echo hello world",
}


class TestFormatToolResult:
    """Core formatting logic."""

    def test_empty_schema_passthrough(self) -> None:
        spec = FakeToolSpec(output_schema={})
        result = format_tool_result(FULL_RESULT, spec, "standard")
        assert result == FULL_RESULT

    def test_raw_level_passthrough(self) -> None:
        spec = FakeToolSpec(output_schema=SHELL_SCHEMA)
        result = format_tool_result(FULL_RESULT, spec, "raw")
        assert result == FULL_RESULT

    def test_minimal_level(self) -> None:
        spec = FakeToolSpec(output_schema=SHELL_SCHEMA)
        result = format_tool_result(FULL_RESULT, spec, "minimal")
        assert result == {"ok": True}

    def test_standard_level(self) -> None:
        spec = FakeToolSpec(output_schema=SHELL_SCHEMA)
        result = format_tool_result(FULL_RESULT, spec, "standard")
        assert result == {"ok": True, "stdout": "hello world\n", "exit_code": 0}

    def test_full_level(self) -> None:
        spec = FakeToolSpec(output_schema=SHELL_SCHEMA)
        result = format_tool_result(FULL_RESULT, spec, "full")
        assert result == FULL_RESULT

    def test_unknown_level_falls_back_to_default(self) -> None:
        spec = FakeToolSpec(output_schema=SHELL_SCHEMA)
        result = format_tool_result(FULL_RESULT, spec, "verbose")
        # Should fall back to x-default-level = "standard"
        assert result == {"ok": True, "stdout": "hello world\n", "exit_code": 0}

    def test_unknown_level_no_default_passthrough(self) -> None:
        schema = {
            "x-output-levels": {"minimal": ["ok"]},
            # No x-default-level
        }
        spec = FakeToolSpec(output_schema=schema)
        result = format_tool_result(FULL_RESULT, spec, "unknown")
        assert result == FULL_RESULT

    def test_no_output_levels_passthrough(self) -> None:
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        spec = FakeToolSpec(output_schema=schema)
        result = format_tool_result(FULL_RESULT, spec, "standard")
        assert result == FULL_RESULT


class TestStringResults:
    """System tools return json.dumps strings."""

    def test_json_string_filtered(self) -> None:
        spec = FakeToolSpec(output_schema=SHELL_SCHEMA)
        result_str = json.dumps(FULL_RESULT)
        result = format_tool_result(result_str, spec, "minimal")
        assert isinstance(result, str)
        assert json.loads(result) == {"ok": True}

    def test_json_string_standard(self) -> None:
        spec = FakeToolSpec(output_schema=SHELL_SCHEMA)
        result_str = json.dumps(FULL_RESULT)
        result = format_tool_result(result_str, spec, "standard")
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert parsed == {"ok": True, "stdout": "hello world\n", "exit_code": 0}

    def test_non_json_string_passthrough(self) -> None:
        spec = FakeToolSpec(output_schema=SHELL_SCHEMA)
        result = format_tool_result("plain text output", spec, "minimal")
        assert result == "plain text output"


class TestEdgeCases:
    """Non-dict, None, and other edge cases."""

    def test_none_result_passthrough(self) -> None:
        spec = FakeToolSpec(output_schema=SHELL_SCHEMA)
        result = format_tool_result(None, spec, "standard")
        assert result is None

    def test_list_result_passthrough(self) -> None:
        spec = FakeToolSpec(output_schema=SHELL_SCHEMA)
        result = format_tool_result([1, 2, 3], spec, "standard")
        assert result == [1, 2, 3]

    def test_integer_result_passthrough(self) -> None:
        spec = FakeToolSpec(output_schema=SHELL_SCHEMA)
        result = format_tool_result(42, spec, "standard")
        assert result == 42


class TestOutputLevelEnum:
    """OutputLevel string enum values."""

    def test_values(self) -> None:
        assert OutputLevel.MINIMAL == "minimal"
        assert OutputLevel.STANDARD == "standard"
        assert OutputLevel.FULL == "full"
        assert OutputLevel.RAW == "raw"
