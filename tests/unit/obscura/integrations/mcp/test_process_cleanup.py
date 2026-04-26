"""Tests for MCP subprocess cleanup — leak detection and reap-by-PID."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from obscura.integrations.mcp import process_cleanup


@pytest.fixture
def long_running_python(tmp_path):
    """Spawn a long-running Python subprocess we can reliably kill."""
    procs: list[subprocess.Popen] = []

    def _start() -> subprocess.Popen:
        # Sleep for 60s — far longer than any test should take. Tests that
        # need a live process kill it explicitly; the fixture's teardown
        # also reaps anything left behind.
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        return proc

    yield _start

    # Teardown: kill any survivors so tests don't leak processes.
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except Exception:
                pass


class TestFindProcessesForCommand:
    def test_empty_command_returns_empty(self) -> None:
        assert process_cleanup.find_processes_for_command("") == []

    def test_finds_known_running_process(self, long_running_python) -> None:
        """Spawn a python sleep subprocess and confirm it's found by command match."""
        proc = long_running_python()
        # Give the process a moment to register in ps.
        time.sleep(0.1)

        # Match on python interpreter path — this should always find at
        # least our subprocess and probably the test runner itself.
        matches = process_cleanup.find_processes_for_command(
            "import time; time.sleep(60)"
        )
        match_pids = [m.pid for m in matches]
        assert proc.pid in match_pids, (
            f"expected pid {proc.pid} in matches; got {match_pids}"
        )

    def test_no_matches_for_unique_string(self) -> None:
        """A command string that no process can be running should give []."""
        # Use a UUID-ish string we control — extremely unlikely collision.
        result = process_cleanup.find_processes_for_command(
            "obscura-test-sentinel-zzzz-9999-not-a-real-command"
        )
        assert result == []

    def test_excludes_own_pid(self) -> None:
        """The current process is filtered out even if its command matches."""
        # Match on python (we're inside python). Result must not include os.getpid().
        matches = process_cleanup.find_processes_for_command("python")
        assert os.getpid() not in [m.pid for m in matches]


class TestDetectOrphans:
    def test_empty_servers_returns_empty(self) -> None:
        assert process_cleanup.detect_orphans([]) == {}

    def test_only_servers_with_matches_appear(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A server whose command has no matches isn't in the output dict."""

        def _fake_find(command: str) -> list[process_cleanup.MCPProcess]:
            if "real" in command:
                return [
                    process_cleanup.MCPProcess(pid=1, state="S+", command=command)
                ]
            return []

        monkeypatch.setattr(
            process_cleanup, "find_processes_for_command", _fake_find
        )

        servers = [
            {"name": "real", "command": "/bin/real-mcp"},
            {"name": "ghost", "command": "/bin/ghost-mcp"},
        ]
        result = process_cleanup.detect_orphans(servers)
        assert "real" in result
        assert "ghost" not in result


class TestCleanupOrphans:
    def test_empty_pid_list(self) -> None:
        result = process_cleanup.cleanup_orphans([])
        assert result.killed == ()
        assert result.failed == ()

    def test_kills_real_process(self, long_running_python) -> None:
        """SIGTERM, then SIGKILL — real process gets reaped."""
        proc = long_running_python()
        time.sleep(0.1)
        result = process_cleanup.cleanup_orphans([proc.pid], grace_seconds=0.5)
        assert proc.pid in result.killed
        # Wait for the kernel to reap so poll() reflects exit.
        proc.wait(timeout=3.0)
        assert proc.poll() is not None  # it's gone

    def test_already_dead_pid_counted_as_killed(self) -> None:
        """A PID that no longer exists doesn't count as a failure."""
        # Spawn + immediately kill so the PID won't exist when cleanup runs.
        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=2.0)
        result = process_cleanup.cleanup_orphans(
            [proc.pid], grace_seconds=0.0
        )
        assert proc.pid in result.killed
        assert proc.pid not in result.failed


class TestPidAlive:
    def test_alive(self, long_running_python) -> None:
        proc = long_running_python()
        time.sleep(0.1)
        assert process_cleanup._pid_alive(proc.pid) is True

    def test_dead(self) -> None:
        # PID 99999999 is essentially guaranteed not to exist.
        assert process_cleanup._pid_alive(99999999) is False
