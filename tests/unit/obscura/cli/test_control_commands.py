"""Tests for CLI control commands."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obscura.cli.control_commands import (
    HeartbeatReport,
    _collect_heartbeat,
    _probe_supervisor_sync,
    cmd_heartbeat,
    cmd_policies,
    cmd_replay,
    cmd_status,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class FakeSessionRecord:
    status: str = "active"
    message_count: int = 5


@dataclass
class FakeTool:
    name: str = "test_tool"
    description: str = "A test tool"


class FakeStore:
    def __init__(self, *, has_session: bool = True, event_count: int = 3) -> None:
        self._has_session = has_session
        self._event_count = event_count
        self._db_path = Path(tempfile.mktemp(suffix=".db"))

    async def get_session(self, session_id: str) -> FakeSessionRecord | None:
        if self._has_session:
            return FakeSessionRecord()
        return None

    async def get_events(self, session_id: str) -> list[Any]:
        return [None] * self._event_count


class FakeClient:
    def list_tools(self) -> list[FakeTool]:
        return [FakeTool(name="bash"), FakeTool(name="read_file")]


@dataclass
class FakeREPLContext:
    session_id: str = "test-session-1234"
    backend: str = "claude"
    model: str = "claude-3-opus"
    tools_enabled: bool = True
    store: Any = field(default_factory=FakeStore)
    client: Any = field(default_factory=FakeClient)
    vector_store: Any = None


# ---------------------------------------------------------------------------
# HeartbeatReport
# ---------------------------------------------------------------------------


class TestHeartbeatReport:
    def test_defaults(self) -> None:
        report = HeartbeatReport()
        assert report.timestamp == ""
        assert report.latency_ms == 0.0
        assert report.session_id == ""
        assert report.tool_names == []
        assert report.events_db_ok is False

    def test_to_dict_keys(self) -> None:
        report = HeartbeatReport(session_id="s1", latency_ms=42.0)
        d = report.to_dict()
        assert d["session_id"] == "s1"
        assert d["latency_ms"] == 42.0
        assert "timestamp" in d
        assert "tool_names" in d
        assert "supervisor_db_exists" in d

    def test_to_json_valid(self) -> None:
        report = HeartbeatReport(session_id="s1", tool_names=["bash"])
        j = report.to_json()
        parsed = json.loads(j)
        assert parsed["session_id"] == "s1"
        assert parsed["tool_names"] == ["bash"]

    def test_to_dict_roundtrip(self) -> None:
        report = HeartbeatReport(
            timestamp="2026-01-01T00:00:00",
            session_id="abc",
            latency_ms=10.5,
            tool_count=3,
            tool_names=["a", "b", "c"],
        )
        d = report.to_dict()
        j = json.dumps(d)
        parsed = json.loads(j)
        assert parsed["tool_count"] == 3
        assert len(parsed["tool_names"]) == 3


# ---------------------------------------------------------------------------
# _collect_heartbeat
# ---------------------------------------------------------------------------


class TestCollectHeartbeat:
    @pytest.mark.asyncio
    async def test_basic_collection(self) -> None:
        ctx = FakeREPLContext()
        report = await _collect_heartbeat(ctx)
        assert report.session_id == "test-session-1234"
        assert report.session_backend == "claude"
        assert report.session_model == "claude-3-opus"
        assert report.tools_enabled is True
        assert report.latency_ms > 0

    @pytest.mark.asyncio
    async def test_session_info_populated(self) -> None:
        ctx = FakeREPLContext()
        report = await _collect_heartbeat(ctx)
        assert report.session_status == "active"
        assert report.message_count == 5

    @pytest.mark.asyncio
    async def test_event_count_populated(self) -> None:
        ctx = FakeREPLContext(store=FakeStore(event_count=7))
        report = await _collect_heartbeat(ctx)
        assert report.event_count == 7

    @pytest.mark.asyncio
    async def test_tool_info_populated(self) -> None:
        ctx = FakeREPLContext()
        report = await _collect_heartbeat(ctx)
        assert report.tool_count == 2
        assert "bash" in report.tool_names
        assert "read_file" in report.tool_names

    @pytest.mark.asyncio
    async def test_no_session_graceful(self) -> None:
        ctx = FakeREPLContext(store=FakeStore(has_session=False))
        report = await _collect_heartbeat(ctx)
        assert report.session_status == ""
        assert report.message_count == 0

    @pytest.mark.asyncio
    async def test_latency_under_200ms(self) -> None:
        ctx = FakeREPLContext()
        report = await _collect_heartbeat(ctx)
        assert report.latency_ms < 200


# ---------------------------------------------------------------------------
# Supervisor probe
# ---------------------------------------------------------------------------


class TestSupervisorProbe:
    def test_no_db_graceful(self) -> None:
        report = HeartbeatReport()
        _probe_supervisor_sync(report, "nonexistent-session")
        assert report.supervisor_db_exists is False
        assert report.supervisor_lock_held is False

    def test_with_lock(self, tmp_path: Path) -> None:
        from obscura.core.supervisor.schema import init_supervisor_schema

        db_path = tmp_path / "supervisor.db"
        conn = sqlite3.connect(str(db_path))
        init_supervisor_schema(conn)

        # Insert a non-expired lock
        now = datetime.now(UTC)
        expires = now + timedelta(hours=1)
        conn.execute(
            "INSERT INTO session_locks (session_id, holder_id, acquired_at, heartbeat_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("sess-1", "holder-abc", now.isoformat(), now.isoformat(), expires.isoformat()),
        )
        conn.commit()
        conn.close()

        report = HeartbeatReport()
        with patch("obscura.cli.control_commands.resolve_obscura_home", return_value=tmp_path):
            _probe_supervisor_sync(report, "sess-1")

        assert report.supervisor_db_exists is True
        assert report.supervisor_lock_held is True
        assert report.supervisor_lock_holder == "holder-abc"

    def test_expired_lock_not_held(self, tmp_path: Path) -> None:
        from obscura.core.supervisor.schema import init_supervisor_schema

        db_path = tmp_path / "supervisor.db"
        conn = sqlite3.connect(str(db_path))
        init_supervisor_schema(conn)

        now = datetime.now(UTC)
        expired = now - timedelta(hours=1)
        conn.execute(
            "INSERT INTO session_locks (session_id, holder_id, acquired_at, heartbeat_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("sess-1", "holder-old", now.isoformat(), now.isoformat(), expired.isoformat()),
        )
        conn.commit()
        conn.close()

        report = HeartbeatReport()
        with patch("obscura.cli.control_commands.resolve_obscura_home", return_value=tmp_path):
            _probe_supervisor_sync(report, "sess-1")

        assert report.supervisor_db_exists is True
        assert report.supervisor_lock_held is False


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


class TestCommandHandlers:
    @pytest.mark.asyncio
    async def test_status_delegates_to_heartbeat(self) -> None:
        """cmd_status should call cmd_heartbeat."""
        ctx = FakeREPLContext()
        with patch("obscura.cli.control_commands.cmd_heartbeat", new_callable=AsyncMock) as mock_hb:
            mock_hb.return_value = None
            await cmd_status("", ctx)
            mock_hb.assert_called_once_with("", ctx)

    @pytest.mark.asyncio
    async def test_heartbeat_json_flag(self) -> None:
        ctx = FakeREPLContext()
        with patch("obscura.cli.control_commands.console") as mock_console:
            result = await cmd_heartbeat("--json", ctx)
            assert result is None
            # Should have called console.print with Syntax object
            mock_console.print.assert_called_once()

    @pytest.mark.asyncio
    async def test_heartbeat_rich_output(self) -> None:
        ctx = FakeREPLContext()
        with patch("obscura.cli.control_commands.console") as mock_console:
            result = await cmd_heartbeat("", ctx)
            assert result is None
            mock_console.print.assert_called_once()

    @pytest.mark.asyncio
    async def test_policies_no_db(self) -> None:
        ctx = FakeREPLContext()
        with patch("obscura.cli.control_commands.resolve_obscura_home", return_value=Path("/nonexistent")):
            with patch("obscura.cli.control_commands.print_info") as mock_info:
                result = await cmd_policies("", ctx)
                assert result is None
                mock_info.assert_called_once()

    @pytest.mark.asyncio
    async def test_replay_missing_arg(self) -> None:
        ctx = FakeREPLContext()
        with patch("obscura.cli.control_commands.print_error") as mock_err:
            result = await cmd_replay("", ctx)
            assert result is None
            mock_err.assert_called_once()
            assert "Usage" in mock_err.call_args[0][0]

    @pytest.mark.asyncio
    async def test_replay_no_db(self) -> None:
        ctx = FakeREPLContext()
        with patch("obscura.cli.control_commands.resolve_obscura_home", return_value=Path("/nonexistent")):
            with patch("obscura.cli.control_commands.print_info") as mock_info:
                result = await cmd_replay("some-run-id", ctx)
                assert result is None
                mock_info.assert_called_once()
