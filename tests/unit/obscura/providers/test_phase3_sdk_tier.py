"""Phase 3 SDK tier filter — opt-in via OBSCURA_PHASE3_SDK_TIER.

Verifies:
- Default OFF: Copilot/Claude session config includes all (filtered)
  tools regardless of tier.
- Opt-in ON: only core tools survive into the session config; the
  observability hook emits a TurnToolStats with the dropped names.

These tests don't talk to the real SDKs — they exercise the filter
logic directly by constructing a list of ToolSpecs and asserting on
the post-filter list. Each provider has the filter inlined in its
session-config builder, so we extract that branch for test-local
exercise. (The integration-side coverage is the regular per-backend
test suite.)
"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

from obscura.core.tool_observability import (
    TurnToolStats,
    clear_observers,
    register_observer,
)
from obscura.core.tool_tiering import CORE_TOOL_NAMES
from obscura.core.types import ToolSpec


def _stub_handler(*_a: Any, **_kw: Any) -> str:
    return ""


def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"description for {name}",
        parameters={"type": "object", "properties": {}},
        handler=_stub_handler,
    )


# Re-implement the filter inline for unit-test purposes; matches the
# logic copied into copilot.py and claude.py session builders.
def _phase3_filter_copilot(tools: list[ToolSpec]) -> list[ToolSpec]:
    return [
        t
        for t in tools
        if t.name in CORE_TOOL_NAMES
        or (
            t.name.startswith("mcp__")
            and (t.name.rsplit("__", 1)[-1] if "__" in t.name else "")
            in CORE_TOOL_NAMES
        )
    ]


def _phase3_filter_claude(tools: list[ToolSpec]) -> list[ToolSpec]:
    return [t for t in tools if t.name in CORE_TOOL_NAMES]


# ---------------------------------------------------------------------------
# Copilot filter behavior
# ---------------------------------------------------------------------------


def test_copilot_filter_keeps_core_drops_deferred() -> None:
    tools = [
        _spec("read_text_file"),  # core
        _spec("jira_create_issue"),  # deferred
        _spec("grep_files"),  # core
    ]
    kept = _phase3_filter_copilot(tools)
    assert sorted(t.name for t in kept) == ["grep_files", "read_text_file"]


def test_copilot_filter_keeps_mcp_aliases_for_core_names() -> None:
    """mcp__obs__run_shell should survive — its suffix matches a core name."""
    tools = [
        _spec("mcp__obs__run_shell"),  # core via suffix
        _spec("mcp__jira__list_issues"),  # deferred suffix
        _spec("read_text_file"),  # core direct
    ]
    kept = _phase3_filter_copilot(tools)
    names = sorted(t.name for t in kept)
    assert "mcp__obs__run_shell" in names
    assert "read_text_file" in names
    assert "mcp__jira__list_issues" not in names


# ---------------------------------------------------------------------------
# Claude filter behavior — no MCP aliasing (Claude registers tools directly
# via in-process MCP, so the prefix is uniform and not a concern here)
# ---------------------------------------------------------------------------


def test_claude_filter_keeps_only_core() -> None:
    tools = [_spec("read_text_file"), _spec("jira_create_issue")]
    kept = _phase3_filter_claude(tools)
    assert [t.name for t in kept] == ["read_text_file"]


# ---------------------------------------------------------------------------
# Env gate behavior — verify the env-var parsing matches what each
# session builder does. (We can't hit the real builder without
# instantiating a backend; this guards the parsing rule itself.)
# ---------------------------------------------------------------------------


def _phase3_enabled() -> bool:
    return os.environ.get("OBSCURA_PHASE3_SDK_TIER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def test_phase3_default_off() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OBSCURA_PHASE3_SDK_TIER", None)
        assert _phase3_enabled() is False


def test_phase3_on_when_env_set_truthy() -> None:
    for value in ("1", "true", "TRUE", "yes", "on"):
        with patch.dict(os.environ, {"OBSCURA_PHASE3_SDK_TIER": value}):
            assert _phase3_enabled() is True


def test_phase3_off_when_env_set_falsy() -> None:
    for value in ("0", "false", "no", "off", ""):
        with patch.dict(os.environ, {"OBSCURA_PHASE3_SDK_TIER": value}):
            assert _phase3_enabled() is False


# ---------------------------------------------------------------------------
# Observability emission shape (used by both backends when phase3 fires)
# ---------------------------------------------------------------------------


def test_observability_payload_for_filtered_session() -> None:
    received: list[TurnToolStats] = []
    clear_observers()
    register_observer(received.append)
    try:
        tools = [
            _spec("read_text_file"),
            _spec("jira_create_issue"),
            _spec("postman_run"),
        ]
        kept = _phase3_filter_claude(tools)
        kept_names = {t.name for t in kept}
        dropped = tuple(t.name for t in tools if t.name not in kept_names)

        from obscura.core.tool_observability import emit_turn_tool_stats

        emit_turn_tool_stats(
            TurnToolStats(
                backend="claude",
                registry_total=len(tools),
                core_count=len(kept),
                discovered_count=0,
                sent_count=len(kept),
                dropped=dropped,
            ),
        )

        assert len(received) == 1
        s = received[0]
        assert s.backend == "claude"
        assert s.registry_total == 3
        assert s.sent_count == 1
        assert sorted(s.dropped) == ["jira_create_issue", "postman_run"]
    finally:
        clear_observers()
