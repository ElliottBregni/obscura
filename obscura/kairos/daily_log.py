"""obscura.kairos.daily_log — Append-only daily log management.

Maintains a chronological log of observations and events per day,
stored at ``~/.obscura/memory/logs/YYYY/MM/YYYY-MM-DD.md``.

These logs are the primary signal source for dream consolidation.
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _log_dir() -> Path:
    """Resolve the daily log root directory."""
    return Path.home() / ".obscura" / "memory" / "logs"


def _log_path(date: datetime.date | None = None) -> Path:
    """Resolve the log file path for a given date (default: today)."""
    d = date or datetime.date.today()
    return _log_dir() / f"{d.year}" / f"{d.month:02d}" / f"{d.isoformat()}.md"


class DailyLog:
    """Append-only daily log for KAIROS observations."""

    def __init__(self, date: datetime.date | None = None) -> None:
        self._date = date or datetime.date.today()
        self._path = _log_path(self._date)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def exists(self) -> bool:
        return self._path.is_file()

    def append(self, entry: str, *, source: str = "kairos") -> None:
        """Append a timestamped entry to today's log."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"- [{now}] ({source}) {entry}\n"

        if not self._path.exists():
            # Create with header.
            header = f"# Daily Log — {self._date.isoformat()}\n\n"
            self._path.write_text(header + line, encoding="utf-8")
        else:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line)

    def read(self) -> str:
        """Read the full log content."""
        if not self._path.is_file():
            return ""
        return self._path.read_text(encoding="utf-8")

    def entry_count(self) -> int:
        """Count entries in today's log."""
        if not self._path.is_file():
            return 0
        return sum(
            1 for line in self._path.read_text().splitlines() if line.startswith("- [")
        )

    @staticmethod
    def recent_logs(days: int = 7) -> list[Path]:
        """Return paths to the most recent N days of logs that exist."""
        today = datetime.date.today()
        paths: list[Path] = []
        for i in range(days):
            d = today - datetime.timedelta(days=i)
            p = _log_path(d)
            if p.is_file():
                paths.append(p)
        return paths
