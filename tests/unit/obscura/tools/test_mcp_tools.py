"""Unit tests for Mcp.mcp_discovery_status and Mcp.mcp_cleanup_orphans.

Both tools are async. Mock strategy:
  - Patch current_tool_context() at the module level.
  - Patch discover_mcp_servers, detect_orphans, cleanup_orphans at the module level.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import obscura.tools.system._mcp as _mcp_mod
from obscura.tools.system._mcp import Mcp

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# mcp_discovery_status
# ---------------------------------------------------------------------------


async def test_mcp_discovery_status_no_context_returns_not_configured() -> None:
    with patch.object(_mcp_mod, "current_tool_context", return_value=None):
        result = json.loads(await Mcp.mcp_discovery_status())

    assert result["ok"] is True
    assert result["configured"] is False


async def test_mcp_discovery_status_context_no_report_returns_not_configured() -> None:
    ctx = MagicMock()
    ctx.mcp_discovery_report = None

    with patch.object(_mcp_mod, "current_tool_context", return_value=ctx):
        result = json.loads(await Mcp.mcp_discovery_status())

    assert result["ok"] is True
    assert result["configured"] is False


async def test_mcp_discovery_status_with_report_returns_report_dict() -> None:
    report = MagicMock()
    report.to_dict.return_value = {
        "ok": True,
        "servers": [{"name": "my-server", "status": "ok"}],
        "total": 1,
        "failed": 0,
    }
    ctx = MagicMock()
    ctx.mcp_discovery_report = report

    with patch.object(_mcp_mod, "current_tool_context", return_value=ctx):
        result = json.loads(await Mcp.mcp_discovery_status())

    assert result["ok"] is True
    assert result["total"] == 1


# ---------------------------------------------------------------------------
# mcp_cleanup_orphans
# ---------------------------------------------------------------------------


async def test_mcp_cleanup_orphans_no_context_returns_error() -> None:
    with patch.object(_mcp_mod, "current_tool_context", return_value=None):
        result = json.loads(await Mcp.mcp_cleanup_orphans())

    assert result["ok"] is False
    assert "no_discovery_report" in result.get("error", "")


async def test_mcp_cleanup_orphans_no_servers_returns_empty() -> None:
    report = MagicMock()
    report.statuses = []
    ctx = MagicMock()
    ctx.mcp_discovery_report = report

    with patch.object(_mcp_mod, "current_tool_context", return_value=ctx):
        result = json.loads(await Mcp.mcp_cleanup_orphans(dry_run=True))

    assert result["ok"] is True
    assert result["scanned"] == []
    # early-exit path: no server names → uses "killed" key (not "would_kill")
    assert result["killed"] == []


async def test_mcp_cleanup_orphans_dry_run_lists_without_killing() -> None:
    server_status = MagicMock()
    server_status.server_name = "test-server"
    report = MagicMock()
    report.statuses = [server_status]
    ctx = MagicMock()
    ctx.mcp_discovery_report = report

    server_cfg = MagicMock()
    server_cfg.name = "test-server"
    server_cfg.command = ["node", "server.js"]

    proc1 = MagicMock()
    proc1.pid = 101
    proc2 = MagicMock()
    proc2.pid = 102

    with (
        patch.object(_mcp_mod, "current_tool_context", return_value=ctx),
        patch.object(_mcp_mod, "discover_mcp_servers", return_value=[server_cfg]),
        patch.object(
            _mcp_mod, "detect_orphans", return_value={"test-server": [proc1, proc2]}
        ),
    ):
        result = json.loads(await Mcp.mcp_cleanup_orphans(dry_run=True))

    assert result["ok"] is True
    assert result["dry_run"] is True
    # Lower-PID proc should be in would_kill; highest PID kept
    assert 101 in result["would_kill"]
    assert 102 not in result["would_kill"]


async def test_mcp_cleanup_orphans_dry_run_false_calls_cleanup() -> None:
    server_status = MagicMock()
    server_status.server_name = "srv"
    report = MagicMock()
    report.statuses = [server_status]
    ctx = MagicMock()
    ctx.mcp_discovery_report = report

    server_cfg = MagicMock()
    server_cfg.name = "srv"
    server_cfg.command = ["node", "s.js"]

    proc1 = MagicMock()
    proc1.pid = 200
    proc2 = MagicMock()
    proc2.pid = 201

    cleanup_result = MagicMock()
    cleanup_result.killed = [200]
    cleanup_result.failed = []

    with (
        patch.object(_mcp_mod, "current_tool_context", return_value=ctx),
        patch.object(_mcp_mod, "discover_mcp_servers", return_value=[server_cfg]),
        patch.object(_mcp_mod, "detect_orphans", return_value={"srv": [proc1, proc2]}),
        patch.object(
            _mcp_mod, "cleanup_orphans", return_value=cleanup_result
        ) as mock_cleanup,
    ):
        result = json.loads(await Mcp.mcp_cleanup_orphans(dry_run=False))

    assert result["ok"] is True
    assert result["dry_run"] is False
    assert result["killed"] == [200]
    mock_cleanup.assert_called_once_with([200])


async def test_mcp_cleanup_orphans_single_match_skipped_without_force() -> None:
    """A server with only one running process should be skipped (active session)."""
    server_status = MagicMock()
    server_status.server_name = "single-srv"
    report = MagicMock()
    report.statuses = [server_status]
    ctx = MagicMock()
    ctx.mcp_discovery_report = report

    server_cfg = MagicMock()
    server_cfg.name = "single-srv"
    server_cfg.command = ["node", "s.js"]

    proc = MagicMock()
    proc.pid = 999

    with (
        patch.object(_mcp_mod, "current_tool_context", return_value=ctx),
        patch.object(_mcp_mod, "discover_mcp_servers", return_value=[server_cfg]),
        patch.object(_mcp_mod, "detect_orphans", return_value={"single-srv": [proc]}),
    ):
        result = json.loads(await Mcp.mcp_cleanup_orphans(dry_run=True, force=False))

    assert result["ok"] is True
    assert result["would_kill"] == []
    assert result["scanned"][0]["skipped"] is not None
