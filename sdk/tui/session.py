"""
sdk.tui.session -- TUI session management.

Manages conversation history, persistence to disk, and session listing.
Sessions are stored as JSON in ~/.obscura/tui_sessions/<id>.json.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from sdk.tui.modes import TUIMode


# ---------------------------------------------------------------------------
# Session directory
# ---------------------------------------------------------------------------


def _sessions_dir() -> Path:
    """Return the sessions directory, creating it if necessary."""
    d = Path.home() / ".obscura" / "tui_sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Conversation turn
# ---------------------------------------------------------------------------


@dataclass
class ConversationTurn:
    """A single turn in the conversation."""

    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime
    mode: TUIMode
    metadata: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "mode": self.mode.value,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationTurn:
        """Deserialize from a dict."""
        return cls(
            role=data["role"],
            content=data["content"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            mode=TUIMode(data.get("mode", "ask")),
            metadata=data.get("metadata", {}),
        )


# ---------------------------------------------------------------------------
# TUI Session
# ---------------------------------------------------------------------------

_MAX_SESSIONS = 50  # Auto-rotate: keep the most recent N sessions


class TUISession:
    """Manages conversation history and persists to disk.

    Each session has a unique ID and stores an ordered list of
    conversation turns. Sessions are saved as JSON to
    ``~/.obscura/tui_sessions/<id>.json``.
    """

    def __init__(
        self,
        session_id: str | None = None,
        backend: str = "copilot",
        model: str | None = None,
    ) -> None:
        self.session_id: str = session_id or uuid.uuid4().hex[:8]
        self.backend: str = backend
        self.model: str | None = model
        self.turns: list[ConversationTurn] = []
        self.created_at: datetime = datetime.now()
        self.updated_at: datetime = datetime.now()
        self._file_path: Path = _sessions_dir() / f"{self.session_id}.json"

    # -- Properties ---------------------------------------------------------

    @property
    def file_path(self) -> Path:
        return self._file_path

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def duration(self) -> float:
        """Session duration in seconds."""
        if not self.turns:
            return 0.0
        return (self.updated_at - self.created_at).total_seconds()

    @property
    def last_mode(self) -> TUIMode:
        """Mode of the most recent turn."""
        if self.turns:
            return self.turns[-1].mode
        return TUIMode.ASK

    # -- Turn management ----------------------------------------------------

    def add_turn(self, turn: ConversationTurn) -> None:
        """Add a conversation turn and update the timestamp."""
        self.turns.append(turn)
        self.updated_at = datetime.now()

    def add_user_turn(
        self,
        content: str,
        mode: TUIMode,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationTurn:
        """Convenience: add a user turn."""
        turn = ConversationTurn(
            role="user",
            content=content,
            timestamp=datetime.now(),
            mode=mode,
            metadata=metadata or {},
        )
        self.add_turn(turn)
        return turn

    def add_assistant_turn(
        self,
        content: str,
        mode: TUIMode,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationTurn:
        """Convenience: add an assistant turn."""
        turn = ConversationTurn(
            role="assistant",
            content=content,
            timestamp=datetime.now(),
            mode=mode,
            metadata=metadata or {},
        )
        self.add_turn(turn)
        return turn

    def clear(self) -> None:
        """Clear all turns (keeps session ID)."""
        self.turns.clear()
        self.updated_at = datetime.now()

    # -- Persistence --------------------------------------------------------

    def save(self) -> None:
        """Save session to disk as JSON."""
        data: dict[str, Any] = {
            "session_id": self.session_id,
            "backend": self.backend,
            "model": self.model,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "turns": [t.to_dict() for t in self.turns],
        }
        self._file_path.parent.mkdir(parents=True, exist_ok=True)

        # Write atomically via temp file
        tmp = self._file_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            tmp.replace(self._file_path)
        except Exception:
            if tmp.exists():
                tmp.unlink()
            raise

    @classmethod
    def load(cls, session_id: str) -> TUISession:
        """Load a session from disk.

        Args:
            session_id: The session ID to load.

        Returns:
            The loaded TUISession.

        Raises:
            FileNotFoundError: If the session file does not exist.
            json.JSONDecodeError: If the session file is corrupt.
        """
        path = _sessions_dir() / f"{session_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Session not found: {session_id}")

        data = json.loads(path.read_text())
        session = cls(
            session_id=data["session_id"],
            backend=data.get("backend", "copilot"),
            model=data.get("model"),
        )
        session.created_at = datetime.fromisoformat(data["created_at"])
        session.updated_at = datetime.fromisoformat(data["updated_at"])
        session.turns = [ConversationTurn.from_dict(t) for t in data.get("turns", [])]
        return session

    @classmethod
    def list_sessions(cls) -> list[dict[str, Any]]:
        """List all saved sessions, most recent first.

        Returns:
            List of dicts with session_id, backend, model, created_at,
            updated_at, turn_count.
        """
        sessions_dir = _sessions_dir()
        results: list[dict[str, Any]] = []

        for path in sessions_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                results.append(
                    {
                        "session_id": data["session_id"],
                        "backend": data.get("backend", "unknown"),
                        "model": data.get("model"),
                        "created_at": data.get("created_at", ""),
                        "updated_at": data.get("updated_at", ""),
                        "turn_count": len(data.get("turns", [])),
                    }
                )
            except (json.JSONDecodeError, KeyError):
                continue

        # Sort by updated_at descending
        results.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return results

    @classmethod
    def delete_session(cls, session_id: str) -> bool:
        """Delete a saved session.

        Returns:
            True if the session was deleted, False if not found.
        """
        path = _sessions_dir() / f"{session_id}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    @classmethod
    def auto_rotate(cls) -> None:
        """Delete oldest sessions to keep at most _MAX_SESSIONS."""
        sessions = cls.list_sessions()
        if len(sessions) <= _MAX_SESSIONS:
            return
        # Delete excess, starting from the oldest
        for info in sessions[_MAX_SESSIONS:]:
            cls.delete_session(info["session_id"])
