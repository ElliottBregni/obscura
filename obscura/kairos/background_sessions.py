"""obscura.kairos.background_sessions — Background session management.

Provides ``ps``, ``logs``, ``attach``, and ``kill`` operations for
background Obscura sessions running as detached processes.

Sessions are tracked via a registry file at ``~/.obscura/bg-sessions.json``.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path.home() / ".obscura" / "bg-sessions.json"
_LOG_DIR = Path.home() / ".obscura" / "bg-logs"


@dataclass
class BackgroundSession:
    """Metadata for a background Obscura session."""

    session_id: str
    pid: int
    command: str
    started_at: float = field(default_factory=time.time)
    status: str = "running"  # running | completed | failed | killed
    log_file: str = ""
    model: str = ""
    cwd: str = ""


class BackgroundSessionRegistry:
    """Registry of background sessions.

    Persists to ``~/.obscura/bg-sessions.json``.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, BackgroundSession] = {}
        self._load()

    def _load(self) -> None:
        """Load registry from disk."""
        if _REGISTRY_PATH.is_file():
            try:
                data = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
                for sid, entry in data.items():
                    self._sessions[sid] = BackgroundSession(**entry)
            except (json.JSONDecodeError, TypeError):
                self._sessions = {}

    def _save(self) -> None:
        """Persist registry to disk."""
        _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {sid: asdict(s) for sid, s in self._sessions.items()}
        _REGISTRY_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def register(self, session: BackgroundSession) -> None:
        """Register a new background session."""
        self._sessions[session.session_id] = session
        self._save()

    def unregister(self, session_id: str) -> None:
        """Remove a session from the registry."""
        self._sessions.pop(session_id, None)
        self._save()

    def update_status(self, session_id: str, status: str) -> None:
        """Update session status."""
        if session_id in self._sessions:
            self._sessions[session_id].status = status
            self._save()

    def list_sessions(self) -> list[BackgroundSession]:
        """List all tracked sessions, pruning dead ones."""
        self._prune_dead()
        return list(self._sessions.values())

    def get(self, session_id: str) -> BackgroundSession | None:
        """Get a session by ID (prefix match supported)."""
        # Exact match.
        if session_id in self._sessions:
            return self._sessions[session_id]
        # Prefix match.
        matches = [s for sid, s in self._sessions.items() if sid.startswith(session_id)]
        return matches[0] if len(matches) == 1 else None

    def _prune_dead(self) -> None:
        """Mark sessions with dead PIDs as failed."""
        changed = False
        for session in self._sessions.values():
            if session.status == "running" and not _is_pid_alive(session.pid):
                session.status = "failed"
                changed = True
        if changed:
            self._save()


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ---------------------------------------------------------------------------
# CLI operations: ps, logs, attach, kill
# ---------------------------------------------------------------------------


def ps() -> list[dict[str, Any]]:
    """List background sessions (``obscura ps``)."""
    registry = BackgroundSessionRegistry()
    sessions = registry.list_sessions()
    return [
        {
            "session_id": s.session_id[:12],
            "pid": s.pid,
            "status": s.status,
            "model": s.model,
            "uptime_s": int(time.time() - s.started_at) if s.status == "running" else 0,
            "command": s.command[:60],
        }
        for s in sessions
    ]


def logs(session_id: str, *, tail: int = 50) -> str:
    """Get recent log output for a background session."""
    registry = BackgroundSessionRegistry()
    session = registry.get(session_id)
    if session is None:
        return f"Session not found: {session_id}"
    if not session.log_file or not Path(session.log_file).is_file():
        return "No log file available"
    lines = Path(session.log_file).read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[-tail:])


def kill_session(session_id: str) -> str:
    """Kill a background session."""
    registry = BackgroundSessionRegistry()
    session = registry.get(session_id)
    if session is None:
        return f"Session not found: {session_id}"
    if session.status != "running":
        return f"Session {session_id[:12]} is already {session.status}"
    try:
        os.kill(session.pid, signal.SIGTERM)
        registry.update_status(session.session_id, "killed")
        return f"Killed session {session.session_id[:12]} (PID {session.pid})"
    except ProcessLookupError:
        registry.update_status(session.session_id, "failed")
        return f"Process already dead (PID {session.pid})"
