"""Unit tests for the current_time system tool."""

from __future__ import annotations

import json

import pytest

from obscura.tools.system import current_time, get_system_tool_specs

pytestmark = pytest.mark.unit


async def test_current_time_returns_live_clock_payload() -> None:
    payload = json.loads(await current_time("UTC"))

    assert payload["ok"] is True
    assert payload["timezone"] == "UTC"
    assert payload["timezone_key"] == "UTC"
    assert payload["utc_offset"] == "+0000"
    assert payload["local_iso"].endswith("+00:00")
    assert payload["utc_iso"].endswith("+00:00")
    assert isinstance(payload["unix_seconds"], float)


async def test_current_time_rejects_unknown_timezone() -> None:
    payload = json.loads(await current_time("No/Such_Zone"))

    assert payload["ok"] is False
    assert payload["error"] == "unknown_timezone"
    assert payload["timezone"] == "No/Such_Zone"


def test_current_time_is_registered_as_system_tool() -> None:
    names = {spec.name for spec in get_system_tool_specs()}

    assert "current_time" in names
