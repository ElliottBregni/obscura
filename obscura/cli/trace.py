from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

from obscura.core.paths import resolve_obscura_home


_lock = Lock()
_logger: logging.Logger | None = None


def _get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    # Place logs/ directory at the project root (two levels up from this file)
    project_root = Path(__file__).resolve().parents[2]
    logs_dir = project_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "trace.log"

    logger = logging.getLogger("obscura.trace")
    logger.setLevel(logging.INFO)
    # Avoid duplicate handlers during reload
    if not any(isinstance(h, RotatingFileHandler) and Path(h.baseFilename).resolve() == log_path.resolve() for h in logger.handlers):
        handler = RotatingFileHandler(str(log_path), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    _logger = logger
    return _logger


def append_event(kind: str, preview: str = "", tool_names: Iterable[str] | None = None, extra: dict[str, Any] | None = None) -> None:
    """Append a single JSONL trace event.

    Fields: ts (ISO8601), kind, preview, tool_names, extra
    """
    logger = _get_logger()
    payload = {
        "ts": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "kind": kind,
        "preview": preview or "",
        "tool_names": list(tool_names) if tool_names else [],
    }
    if extra:
        payload["extra"] = extra
    line = json.dumps(payload, default=str, ensure_ascii=False)
    # write atomically under lock so lines don't interleave
    with _lock:
        logger.info(line)


def tail_entries(n: int = 50) -> list[dict[str, Any]]:
    """Return the last n parsed JSON entries from the trace log."""
    project_root = Path(__file__).resolve().parents[2]
    log_path = project_root / "logs" / "trace.log"
    if not log_path.exists():
        return []
    from collections import deque

    dq = deque(maxlen=n)
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    dq.append(json.loads(line))
                except Exception:
                    # keep raw line if JSON parse fails
                    dq.append({"raw": line})
    except Exception:
        return []
    return list(dq)


def tail_pretty(n: int = 50) -> str:
    entries = tail_entries(n)
    lines = []
    for e in entries:
        ts = e.get("ts", "?")
        kind = e.get("kind", "?")
        preview = e.get("preview", "")
        tools = ",".join(e.get("tool_names", []))
        lines.append(f"[{ts}] {kind} ({tools}) {preview}")
    return "\n".join(lines)
