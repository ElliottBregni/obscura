"""obscura.integrations.mcp.process_cleanup — Detect and reap leaked MCP subprocesses.

Claude Agent SDK launches one stdio subprocess per configured external
MCP server. When a session ends mid-flight (timeout, kill, force-quit),
those subprocesses can be left running — sitting idle, sometimes
suspended (state ``T``), eating descriptors and confusing later sessions.

This module gives obscura tools and the discovery flow a way to:

* List currently-running processes that match a configured MCP server's
  command path (``find_processes_for_command``).
* Spot leaks across all configured servers (``detect_orphans``).
* Reap leaks safely — SIGTERM, then SIGKILL after a grace period
  (``cleanup_orphans``).

We deliberately don't run cleanup automatically at session start: an
in-flight session in another shell shares this process namespace, and
killing its MCP subprocess from underneath it would be worse than the
leak. The discovery flow logs a warning when leaks are detected; cleanup
is an explicit operation, exposed as a system tool.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MCPProcess:
    """One running process matching an MCP server's command path."""

    pid: int
    state: str  # macOS / Linux ps STAT column
    command: str

    @property
    def is_stopped(self) -> bool:
        """True for processes in state ``T`` — SIGSTOP'd or being traced."""
        return self.state.startswith("T")


def find_processes_for_command(
    command: str,
    *,
    descendants_of: int | None = None,
) -> list[MCPProcess]:
    """Return every running process whose command path matches *command*.

    Uses ``ps`` (universally available on macOS / Linux) so we don't pull
    in psutil as a hard dep. Returns an empty list when ``ps`` is missing
    or fails — detection is best-effort.

    *descendants_of*, when set, restricts matches to the process subtree
    rooted at the given PID. This lets the lifecycle hook in
    ``ClaudeBackend`` reap only subprocesses spawned by *its* SDK call,
    leaving alone any owned by concurrent obscura sessions in other shells.
    """
    if not command:
        return []
    if shutil.which("ps") is None:
        return []

    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,stat=,command="],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("ps invocation failed: %s", exc)
        return []

    allowed: set[int] | None = None
    if descendants_of is not None:
        allowed = build_descendant_set(descendants_of)

    own_pid = os.getpid()
    matches: list[MCPProcess] = []
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        # ps output: ``<pid> <stat> <command...>`` — split into 3 parts.
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_str, state, cmd = parts
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid == own_pid:
            continue
        if command not in cmd:
            continue
        if allowed is not None and pid not in allowed:
            continue
        matches.append(MCPProcess(pid=pid, state=state, command=cmd))
    return matches


def _read_pid_ppid_map() -> dict[int, int]:
    """Map every running ``pid -> ppid``. Empty dict on any ps failure."""
    if shutil.which("ps") is None:
        return {}
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("ps -axo pid,ppid failed: %s", exc)
        return {}

    out: dict[int, int] = {}
    for raw in proc.stdout.splitlines():
        parts = raw.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        out[pid] = ppid
    return out


def build_descendant_set(root_pid: int) -> set[int]:
    """Return the set of PIDs in the subtree rooted at *root_pid* (inclusive).

    BFS over the parent map. Robust to cycles (which shouldn't happen in
    real ps output but cost nothing to defend against).
    """
    parents = _read_pid_ppid_map()
    if not parents:
        return {root_pid}

    children: dict[int, list[int]] = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)

    out = {root_pid}
    stack = [root_pid]
    while stack:
        node = stack.pop()
        for c in children.get(node, []):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


def detect_orphans(
    mcp_servers: list[dict[str, Any]],
) -> dict[str, list[MCPProcess]]:
    """Map each configured server name → matching live processes.

    A "leak" is anything more than the single subprocess Claude SDK
    expects to keep alive per session. Multiple matches almost always
    mean older subprocesses weren't reaped.
    """
    by_server: dict[str, list[MCPProcess]] = {}
    for server in mcp_servers:
        name = str(server.get("name") or "unknown")
        command = str(server.get("command") or "")
        if not command:
            continue
        procs = find_processes_for_command(command)
        if procs:
            by_server[name] = procs
    return by_server


@dataclass(frozen=True)
class CleanupResult:
    """Outcome of a cleanup pass."""

    killed: tuple[int, ...]
    failed: tuple[int, ...]


def cleanup_orphans(
    pids: list[int],
    *,
    grace_seconds: float = 1.0,
) -> CleanupResult:
    """Send SIGTERM to each PID, then SIGKILL anything still alive after grace.

    Returns the PIDs we successfully reaped and the PIDs we couldn't
    (already gone, permission denied, …). Never raises — cleanup is a
    best-effort housekeeping operation.
    """
    if not pids:
        return CleanupResult(killed=(), failed=())

    sent_term: list[int] = []
    failed: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            sent_term.append(pid)
        except ProcessLookupError:
            # Already gone — count as success.
            sent_term.append(pid)
        except PermissionError:
            failed.append(pid)
        except OSError as exc:
            logger.debug("SIGTERM to %d failed: %s", pid, exc)
            failed.append(pid)

    if grace_seconds > 0:
        time.sleep(grace_seconds)

    killed: list[int] = []
    for pid in sent_term:
        if not _pid_alive(pid):
            killed.append(pid)
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except ProcessLookupError:
            killed.append(pid)
        except OSError:
            failed.append(pid)

    return CleanupResult(killed=tuple(killed), failed=tuple(failed))


def _pid_alive(pid: int) -> bool:
    """Probe whether *pid* still exists (no signal sent)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't own it.
        return True
    except OSError:
        return False
    return True
