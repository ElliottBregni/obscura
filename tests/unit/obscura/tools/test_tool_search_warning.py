"""Tests for ``tool_search``'s phase-3 warning behavior.

When ``OBSCURA_PHASE3_SDK_TIER=1`` is active and the model surfaces a
deferred tool via ``tool_search(query='select:<name>')``, the response
should warn that the tool isn't loaded into the current SDK session
and suggest using ``OBSCURA_PHASE3_EXTRA_CORE``.

When phase 3 is off, no warning. When the tool is in the effective
core set (either base CORE_TOOL_NAMES or an EXTRA_CORE match), no
warning either.
"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from unittest.mock import patch

from obscura.core.types import ToolSpec
from obscura.tools.system import Registry


def _stub_handler(*_a: Any, **_kw: Any) -> str:
    return ""


def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"description for {name}",
        parameters={"type": "object", "properties": {}},
        handler=_stub_handler,
    )


class _FakeRegistry:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self._by_name = {s.name: s for s in specs}

    def all(self) -> list[ToolSpec]:
        return list(self._by_name.values())

    def get(self, name: str) -> ToolSpec | None:
        return self._by_name.get(name)


def _run_tool_search(query: str, registry: _FakeRegistry) -> dict[str, Any]:
    """Invoke tool_search with a fake registry installed."""
    Registry.set_tool_registry(registry)
    try:
        result = asyncio.run(Registry.tool_search(query))
        return json.loads(result)
    finally:
        Registry.set_tool_registry(None)


def test_no_warning_when_phase3_off() -> None:
    reg = _FakeRegistry([_spec("jira_create_issue"), _spec("read_text_file")])
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OBSCURA_PHASE3_SDK_TIER", None)
        os.environ.pop("OBSCURA_PHASE3_EXTRA_CORE", None)
        result = _run_tool_search("select:jira_create_issue", reg)
    assert result["ok"] is True
    assert "warning" not in result


def test_warning_when_phase3_on_and_tool_is_deferred() -> None:
    reg = _FakeRegistry([_spec("jira_create_issue"), _spec("read_text_file")])
    with patch.dict(os.environ, {"OBSCURA_PHASE3_SDK_TIER": "1"}):
        os.environ.pop("OBSCURA_PHASE3_EXTRA_CORE", None)
        result = _run_tool_search("select:jira_create_issue", reg)
    assert "warning" in result
    assert "jira_create_issue" in result["warning"]
    assert "OBSCURA_PHASE3_EXTRA_CORE" in result["warning"]


def test_no_warning_when_tool_is_core() -> None:
    """Core tools are loaded with the SDK regardless of phase 3."""
    reg = _FakeRegistry([_spec("read_text_file"), _spec("jira_create_issue")])
    with patch.dict(os.environ, {"OBSCURA_PHASE3_SDK_TIER": "1"}):
        os.environ.pop("OBSCURA_PHASE3_EXTRA_CORE", None)
        result = _run_tool_search("select:read_text_file", reg)
    assert "warning" not in result


def test_no_warning_when_tool_in_extra_core() -> None:
    """A deferred tool that's in EXTRA_CORE is loaded — no warning."""
    reg = _FakeRegistry([_spec("jira_create_issue"), _spec("postman_run")])
    with patch.dict(
        os.environ,
        {
            "OBSCURA_PHASE3_SDK_TIER": "1",
            "OBSCURA_PHASE3_EXTRA_CORE": "jira_create_issue",
        },
    ):
        result = _run_tool_search("select:jira_create_issue", reg)
    assert "warning" not in result


def test_warning_with_glob_extra_core() -> None:
    """jira_* glob loads jira_create but not postman_run — warn for postman."""
    reg = _FakeRegistry([_spec("jira_create"), _spec("postman_run")])
    with patch.dict(
        os.environ,
        {
            "OBSCURA_PHASE3_SDK_TIER": "1",
            "OBSCURA_PHASE3_EXTRA_CORE": "jira_*",
        },
    ):
        # jira_create is covered by the glob — no warning.
        r1 = _run_tool_search("select:jira_create", reg)
        assert "warning" not in r1
        # postman_run isn't covered — warn.
        r2 = _run_tool_search("select:postman_run", reg)
        assert "warning" in r2
        assert "postman_run" in r2["warning"]


def test_warning_lists_multiple_uncallable_tools() -> None:
    reg = _FakeRegistry([_spec("jira_create"), _spec("postman_run")])
    with patch.dict(os.environ, {"OBSCURA_PHASE3_SDK_TIER": "1"}):
        os.environ.pop("OBSCURA_PHASE3_EXTRA_CORE", None)
        result = _run_tool_search("select:jira_create,postman_run", reg)
    assert "warning" in result
    assert "jira_create" in result["warning"]
    assert "postman_run" in result["warning"]
