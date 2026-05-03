"""Integration tests for the native host lifecycle.

These tests spawn the actual host process and exercise the wire protocol.
Marked as 'browser' so they can be skipped in fast CI runs.
"""

from __future__ import annotations

import asyncio

import pytest

from .conftest import HostProcess

pytestmark = [pytest.mark.asyncio, pytest.mark.browser]


class TestHostBoot:
    async def test_ready_frame_on_connect(self, host: HostProcess) -> None:
        """Host should emit a 'ready' frame immediately on startup."""
        ready = await host.recv_until("ready", timeout=30)
        assert ready["type"] == "ready"
        assert "version" in ready
        assert "pid" in ready
        assert isinstance(ready.get("commands"), list)
        assert isinstance(ready.get("skills"), list)
        assert isinstance(ready.get("at_commands"), list)
        assert isinstance(ready.get("backends"), list)

    async def test_ping_pong(self, host: HostProcess) -> None:
        """Host should respond to ping with pong."""
        await host.recv_until("ready", timeout=30)
        await host.send({"type": "ping", "id": "test-ping-1"})
        pong = await host.recv_until("pong")
        assert pong["id"] == "test-ping-1"

    async def test_shutdown(self, host: HostProcess) -> None:
        """Host should exit cleanly on shutdown message."""
        await host.recv_until("ready", timeout=30)
        await host.send({"type": "shutdown"})
        # Process should exit
        exit_code = await asyncio.wait_for(host.proc.wait(), timeout=10)
        assert exit_code == 0 or exit_code is None

    async def test_empty_prompt_error(self, host: HostProcess) -> None:
        """Sending an empty prompt should return an error frame."""
        await host.recv_until("ready", timeout=30)
        await host.send({"type": "send", "id": "empty-1", "prompt": ""})
        err = await host.recv_until("error")
        assert "empty" in err.get("message", "").lower() or "Empty" in err.get(
            "message", ""
        )


class TestCommandDispatch:
    async def test_slash_status(self, host: HostProcess) -> None:
        """The /status command should complete with a done or error frame."""
        await host.recv_until("ready", timeout=30)
        await host.send({"type": "command", "id": "cmd-1", "raw": "/status"})
        # Consume frames until we get a terminal frame for this command.
        # In environments without a real backend the session create fails,
        # which produces an error frame instead of done — both are valid.
        msg = await host.recv(timeout=30)
        while msg.get("type") not in ("done", "error") or msg.get("id") != "cmd-1":
            msg = await host.recv(timeout=30)
        assert msg["id"] == "cmd-1"
        assert msg["type"] in ("done", "error")

    async def test_unknown_command(self, host: HostProcess) -> None:
        """An unknown command should still return done (or error), not hang."""
        await host.recv_until("ready", timeout=30)
        await host.send(
            {
                "type": "command",
                "id": "cmd-2",
                "raw": "/nonexistent_command_xyz",
            }
        )
        # Should get done or error within timeout
        msg = await host.recv(timeout=15)
        while msg.get("type") not in ("done", "error"):
            msg = await host.recv(timeout=15)
        assert msg["type"] in ("done", "error")
