"""obscura.core.commit_attribution — Track AI vs human contribution per file.

Records which files were created or modified by the AI agent during
a session, enabling attribution tracking for compliance and auditing.

Attribution data stored at ``~/.obscura/attribution.json``.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ATTRIBUTION_PATH = Path.home() / ".obscura" / "attribution.json"


@dataclass
class FileAttribution:
    """Attribution record for a single file."""

    path: str
    agent_lines_added: int = 0
    agent_lines_removed: int = 0
    human_lines_added: int = 0
    human_lines_removed: int = 0
    last_modified_by: str = "unknown"  # "agent" or "human"
    last_modified_at: float = field(default_factory=time.time)


class CommitAttributionTracker:
    """Track AI contribution per file across a session.

    Usage::

        tracker = CommitAttributionTracker()
        tracker.record_agent_edit("src/main.py", lines_added=10, lines_removed=3)
        tracker.save()
    """

    def __init__(self) -> None:
        self._files: dict[str, FileAttribution] = {}
        self._session_start = time.time()
        self._load()

    def _load(self) -> None:
        """Load existing attribution data."""
        if _ATTRIBUTION_PATH.is_file():
            try:
                data = json.loads(_ATTRIBUTION_PATH.read_text(encoding="utf-8"))
                for path, entry in data.items():
                    self._files[path] = FileAttribution(**entry)
            except (json.JSONDecodeError, TypeError):
                logger.debug("suppressed exception in _load", exc_info=True)

    def save(self) -> None:
        """Persist attribution data to disk."""
        _ATTRIBUTION_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        for path, attr in self._files.items():
            data[path] = {
                "path": attr.path,
                "agent_lines_added": attr.agent_lines_added,
                "agent_lines_removed": attr.agent_lines_removed,
                "human_lines_added": attr.human_lines_added,
                "human_lines_removed": attr.human_lines_removed,
                "last_modified_by": attr.last_modified_by,
                "last_modified_at": attr.last_modified_at,
            }
        _ATTRIBUTION_PATH.write_text(
            json.dumps(data, indent=2) + "\n",
            encoding="utf-8",
        )

    def record_agent_edit(
        self,
        file_path: str,
        *,
        lines_added: int = 0,
        lines_removed: int = 0,
    ) -> None:
        """Record that the agent modified a file."""
        attr = self._files.get(file_path)
        if attr is None:
            attr = FileAttribution(path=file_path)
            self._files[file_path] = attr
        attr.agent_lines_added += lines_added
        attr.agent_lines_removed += lines_removed
        attr.last_modified_by = "agent"
        attr.last_modified_at = time.time()

    def record_human_edit(
        self,
        file_path: str,
        *,
        lines_added: int = 0,
        lines_removed: int = 0,
    ) -> None:
        """Record that a human modified a file."""
        attr = self._files.get(file_path)
        if attr is None:
            attr = FileAttribution(path=file_path)
            self._files[file_path] = attr
        attr.human_lines_added += lines_added
        attr.human_lines_removed += lines_removed
        attr.last_modified_by = "human"
        attr.last_modified_at = time.time()

    def get_attribution(self, file_path: str) -> FileAttribution | None:
        """Get attribution for a specific file."""
        return self._files.get(file_path)

    def summary(self) -> dict[str, Any]:
        """Generate attribution summary."""
        total_agent_added = sum(a.agent_lines_added for a in self._files.values())
        total_agent_removed = sum(a.agent_lines_removed for a in self._files.values())
        total_human_added = sum(a.human_lines_added for a in self._files.values())
        total_human_removed = sum(a.human_lines_removed for a in self._files.values())
        total_agent = total_agent_added + total_agent_removed
        total_human = total_human_added + total_human_removed
        total = total_agent + total_human
        agent_pct = (total_agent / total * 100) if total > 0 else 0.0
        return {
            "files_tracked": len(self._files),
            "agent_lines": total_agent,
            "human_lines": total_human,
            "agent_percentage": round(agent_pct, 1),
        }

    def reset(self) -> None:
        """Clear all attribution data."""
        self._files.clear()


_tracker: CommitAttributionTracker | None = None


def get_attribution_tracker() -> CommitAttributionTracker:
    """Return the global attribution tracker singleton."""
    global _tracker
    if _tracker is None:
        _tracker = CommitAttributionTracker()
    return _tracker
