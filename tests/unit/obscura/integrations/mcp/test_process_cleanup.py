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


class TestBuildDescendantSet:
    def test_root_in_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The subtree always contains the root pid."""
        monkeypatch.setattr(
            process_cleanup,
            "_read_pid_ppid_map",
            lambda: {1: 0, 100: 1, 200: 100},
        )
        result = process_cleanup.build_descendant_set(100)
        assert 100 in result

    def test_walks_full_subtree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Tree:
        #   1 -> 100 -> 200 -> 300
        #         \-> 250
        #   1 -> 999  (sibling, not in subtree)
        monkeypatch.setattr(
            process_cleanup,
            "_read_pid_ppid_map",
            lambda: {1: 0, 100: 1, 200: 100, 300: 200, 250: 100, 999: 1},
        )
        result = process_cleanup.build_descendant_set(100)
        assert result == {100, 200, 250, 300}

    def test_empty_ps_falls_back_to_singleton(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ps fails, the descendant set is just the root."""
        monkeypatch.setattr(process_cleanup, "_read_pid_ppid_map", lambda: {})
        assert process_cleanup.build_descendant_set(42) == {42}


class TestFindProcessesForCommandDescendantsFilter:
    def test_filters_to_subtree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only processes inside the descendants_of subtree are returned."""
        # Two processes match the command, but only pid 200 is a descendant
        # of root pid 100. Pid 500 is a sibling owned by another session.
        own_pid = os.getpid()

        class _Result:
            stdout = (
                "200 S+ /bin/svc\n"
                "500 S+ /bin/svc\n"
                f"{own_pid} R+ /bin/svc\n"  # filtered out: own pid
            )

        monkeypatch.setattr(process_cleanup.shutil, "which", lambda _name: "/bin/ps")
        monkeypatch.setattr(
            process_cleanup.subprocess,
            "run",
            lambda *args, **kwargs: _Result(),
        )
        # Subtree of 100 = {100, 200}.
        monkeypatch.setattr(
            process_cleanup, "build_descendant_set", lambda root: {100, 200}
        )

        result = process_cleanup.find_processes_for_command(
            "svc", descendants_of=100
        )
        pids = [m.pid for m in result]
        assert pids == [200]
        assert 500 not in pids


class TestClaudeBackendReap:
    """ClaudeBackend.start() snapshots; .stop() reaps PIDs that appeared during the session."""

    @pytest.mark.asyncio
    async def test_reap_kills_only_new_descendants(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from obscura.integrations.mcp import process_cleanup as pc
        from obscura.providers.claude import ClaudeBackend

        # Track every PID we'd kill.
        killed: list[int] = []

        def _fake_cleanup(
            pids: list[int], *, grace_seconds: float = 1.0
        ) -> pc.CleanupResult:
            killed.extend(pids)
            return pc.CleanupResult(killed=tuple(pids), failed=())

        monkeypatch.setattr(pc, "cleanup_orphans", _fake_cleanup)

        # Subtree contains everything except pid 999 (concurrent session).
        monkeypatch.setattr(
            pc,
            "build_descendant_set",
            lambda _root: {os.getpid(), 100, 200, 300},
        )

        # ps view at "stop time": 200 (baseline), 300 (new + ours), 999 (new + concurrent).
        find_calls: list[tuple[str, dict]] = []

        def _fake_find(
            command: str,
            *,
            descendants_of: int | None = None,
        ) -> list[pc.MCPProcess]:
            find_calls.append((command, {"descendants_of": descendants_of}))
            return [
                pc.MCPProcess(pid=200, state="S+", command=command),
                pc.MCPProcess(pid=300, state="S+", command=command),
                pc.MCPProcess(pid=999, state="S+", command=command),
            ]

        monkeypatch.setattr(pc, "find_processes_for_command", _fake_find)

        # Build a backend without going through real auth/SDK setup.
        backend = ClaudeBackend.__new__(ClaudeBackend)
        backend._mcp_servers = [
            {"name": "prog", "command": "/bin/prog-mcp"},
        ]
        backend._mcp_pids_at_start = {200}  # 200 was already there

        await backend._reap_session_mcp_subprocesses()

        # Killed: 300 (new + in our subtree). Not 200 (baseline). Not 999 (not in subtree).
        assert killed == [300]
