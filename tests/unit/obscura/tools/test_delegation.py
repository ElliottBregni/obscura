"""Unit tests for obscura.tools.delegation — make_task_tool + gate checks.

Tests exercise the _task_handler closure via the returned ToolSpec.handler.
All external dependencies (PeerRegistry, EventStore, inject_subagent_context,
agent.run_loop) are mocked.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obscura.tools.delegation import (
    DelegationContext,
    make_task_tool,
    _resolve_by_name_or_id,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(**overrides: object) -> DelegationContext:
    defaults: dict = {
        "can_delegate": True,
        "max_delegation_depth": 3,
        "current_depth": 0,
        "delegate_allowlist": [],
        "peer_registry": None,
        "event_store": None,
    }
    defaults.update(overrides)
    return DelegationContext(**defaults)


# ---------------------------------------------------------------------------
# Gate: delegation disabled
# ---------------------------------------------------------------------------


async def test_task_tool_delegation_disabled_returns_error() -> None:
    ctx = _make_ctx(can_delegate=False)
    spec = make_task_tool(ctx)

    result = json.loads(await spec.handler(prompt="do something"))

    assert result["ok"] is False
    assert result["error"] == "delegation_disabled"


# ---------------------------------------------------------------------------
# Gate: max depth exceeded
# ---------------------------------------------------------------------------


async def test_task_tool_max_depth_exceeded_returns_error() -> None:
    ctx = _make_ctx(current_depth=3, max_delegation_depth=3)
    spec = make_task_tool(ctx)

    result = json.loads(await spec.handler(prompt="do something"))

    assert result["ok"] is False
    assert result["error"] == "max_depth_exceeded"


# ---------------------------------------------------------------------------
# Gate: target not in allowlist
# ---------------------------------------------------------------------------


async def test_task_tool_target_not_in_allowlist_returns_error() -> None:
    ctx = _make_ctx(delegate_allowlist=["researcher", "code-reviewer"])
    spec = make_task_tool(ctx)

    result = json.loads(await spec.handler(prompt="task", target="hacker"))

    assert result["ok"] is False
    assert result["error"] == "target_not_allowed"


async def test_task_tool_target_in_allowlist_proceeds() -> None:
    registry = MagicMock()
    registry.resolve.return_value = (
        None  # still fails at no_peer_found, but gets past allowlist
    )
    registry.discover.return_value = []
    ctx = _make_ctx(delegate_allowlist=["researcher"], peer_registry=registry)
    spec = make_task_tool(ctx)

    result = json.loads(await spec.handler(prompt="task", target="researcher"))

    # Gets past allowlist gate, fails at target_not_found
    assert result["error"] != "target_not_allowed"


# ---------------------------------------------------------------------------
# Gate: no peer registry
# ---------------------------------------------------------------------------


async def test_task_tool_no_peer_registry_returns_error() -> None:
    ctx = _make_ctx(peer_registry=None)
    spec = make_task_tool(ctx)

    result = json.loads(await spec.handler(prompt="task", target="researcher"))

    assert result["ok"] is False
    assert result["error"] == "no_peer_registry"


# ---------------------------------------------------------------------------
# Gate: target not found in registry
# ---------------------------------------------------------------------------


async def test_task_tool_target_not_found_returns_error() -> None:
    registry = MagicMock()
    registry.resolve.return_value = None
    registry.discover.return_value = []
    ctx = _make_ctx(peer_registry=registry)
    spec = make_task_tool(ctx)

    result = json.loads(await spec.handler(prompt="task", target="ghost"))

    assert result["ok"] is False
    assert result["error"] == "target_not_found"


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


async def test_task_tool_success_returns_result() -> None:
    agent = MagicMock()
    agent.run_loop = AsyncMock(return_value="analysis done")

    registry = MagicMock()
    registry.resolve.return_value = agent
    registry.discover.return_value = []

    ctx = _make_ctx(peer_registry=registry)
    spec = make_task_tool(ctx)

    import obscura.tools.delegation as _del_mod

    with patch.object(_del_mod, "inject_subagent_context"):
        result = json.loads(await spec.handler(prompt="analyze code", target="analyst"))

    assert result["ok"] is True
    assert "analysis done" in result["result"]
    assert "session_id" in result


async def test_task_tool_success_creates_child_session() -> None:
    agent = MagicMock()
    agent.run_loop = AsyncMock(return_value="done")

    registry = MagicMock()
    registry.resolve.return_value = agent

    event_store = MagicMock()
    event_store.create_session = AsyncMock()

    ctx = _make_ctx(
        peer_registry=registry,
        event_store=event_store,
        session_id="parent-sess",
    )
    spec = make_task_tool(ctx)

    import obscura.tools.delegation as _del_mod

    with patch.object(_del_mod, "inject_subagent_context"):
        await spec.handler(prompt="task", target="analyst")

    event_store.create_session.assert_called_once()
    call_kwargs = event_store.create_session.call_args
    assert call_kwargs[1].get(
        "parent_session_id"
    ) == "parent-sess" or "parent-sess" in str(call_kwargs)


# ---------------------------------------------------------------------------
# Error: inject_subagent_context fails
# ---------------------------------------------------------------------------


async def test_task_tool_inject_failure_aborts_delegation() -> None:
    agent = MagicMock()
    agent.run_loop = AsyncMock(return_value="done")

    registry = MagicMock()
    registry.resolve.return_value = agent

    ctx = _make_ctx(peer_registry=registry)
    spec = make_task_tool(ctx)

    import obscura.tools.delegation as _del_mod

    with patch.object(
        _del_mod,
        "inject_subagent_context",
        side_effect=RuntimeError("injection failed"),
    ):
        result = json.loads(await spec.handler(prompt="task", target="analyst"))

    assert result["ok"] is False
    assert "delegation_aborted" in result["error"]


# ---------------------------------------------------------------------------
# Error: agent.run_loop raises
# ---------------------------------------------------------------------------


async def test_task_tool_run_loop_exception_returns_error() -> None:
    agent = MagicMock()
    agent.run_loop = AsyncMock(side_effect=RuntimeError("agent crashed"))

    registry = MagicMock()
    registry.resolve.return_value = agent

    ctx = _make_ctx(peer_registry=registry)
    spec = make_task_tool(ctx)

    import obscura.tools.delegation as _del_mod

    with patch.object(_del_mod, "inject_subagent_context"):
        result = json.loads(await spec.handler(prompt="task", target="analyst"))

    assert result["ok"] is False
    assert result["error"] == "delegation_failed"
    assert "agent crashed" in result["message"]


# ---------------------------------------------------------------------------
# Spec structure
# ---------------------------------------------------------------------------


def test_make_task_tool_spec_has_correct_name() -> None:
    spec = make_task_tool(_make_ctx())

    assert spec.name == "task"
    assert callable(spec.handler)
    assert "prompt" in spec.parameters.get("required", [])


# ---------------------------------------------------------------------------
# _resolve_by_name_or_id
# ---------------------------------------------------------------------------


def test_resolve_by_name_or_id_finds_by_id() -> None:
    agent = MagicMock()
    registry = MagicMock()
    registry.resolve.return_value = agent

    result = _resolve_by_name_or_id(registry, "agent-id-123")

    assert result is agent


def test_resolve_by_name_or_id_finds_by_name() -> None:
    agent = MagicMock()
    registry = MagicMock()
    registry.resolve.side_effect = lambda x: (
        agent if x is not None and hasattr(x, "name") else None
    )
    ref = MagicMock()
    ref.name = "my-researcher"
    registry.discover.return_value = [ref]

    result = _resolve_by_name_or_id(registry, "my-researcher")

    assert result is agent


def test_resolve_by_name_or_id_returns_none_when_not_found() -> None:
    registry = MagicMock()
    registry.resolve.return_value = None
    registry.discover.return_value = []

    result = _resolve_by_name_or_id(registry, "nobody")

    assert result is None
