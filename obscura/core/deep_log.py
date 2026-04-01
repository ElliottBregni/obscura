"""
obscura.core.deep_log — Deep structured logging for debugging and diagnostics.

Provides a centralized logging system that captures:
  - Every tool call (name, args summary, duration, result status)
  - Every API request (model, tokens in/out, latency)
  - Every agent event (kind, turn, metadata)
  - Session lifecycle events (start, compact, dream, end)

Logs are structured JSON, written to ``~/.obscura/logs/`` with rotation.

Usage::

    from obscura.core.deep_log import dlog

    dlog.tool_call("read_text_file", {"path": "foo.py"}, duration_ms=42, ok=True)
    dlog.api_request("claude-sonnet-4-5", input_tokens=1500, output_tokens=300)
    dlog.event("session_start", session_id="abc123")
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOG_DIR = Path.home() / ".obscura" / "logs"
_MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB per file
_MAX_LOG_FILES = 5  # Keep 5 rotated files


class DeepLogger:
    """Structured JSON logger for deep debugging.

    Each log entry is a single JSON line with:
    - ``ts``: Unix timestamp
    - ``type``: Event type (tool_call, api_request, event, error)
    - ``data``: Event-specific payload
    """

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self._log_file: Path | None = None
        self._file_handle: Any = None
        self._buffer: list[dict[str, Any]] = []
        self._buffer_limit = 50  # Flush every N entries
        self._total_entries = 0

    def _ensure_file(self) -> None:
        """Lazily open the log file."""
        if self._file_handle is not None:
            return
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._log_file = _LOG_DIR / "deep.jsonl"
        self._rotate_if_needed()
        self._file_handle = self._log_file.open("a", encoding="utf-8")

    def _rotate_if_needed(self) -> None:
        """Rotate log file if it exceeds max size."""
        if self._log_file is None or not self._log_file.exists():
            return
        if self._log_file.stat().st_size < _MAX_LOG_SIZE:
            return
        # Rotate: deep.jsonl → deep.1.jsonl → ... → deep.5.jsonl (deleted)
        for i in range(_MAX_LOG_FILES, 0, -1):
            old = _LOG_DIR / f"deep.{i}.jsonl"
            if i == _MAX_LOG_FILES and old.exists():
                old.unlink()
            elif old.exists():
                old.rename(_LOG_DIR / f"deep.{i + 1}.jsonl")
        self._log_file.rename(_LOG_DIR / "deep.1.jsonl")

    def _write(self, entry: dict[str, Any]) -> None:
        """Write a log entry."""
        if not self._enabled:
            return
        entry["ts"] = time.time()
        self._buffer.append(entry)
        self._total_entries += 1
        if len(self._buffer) >= self._buffer_limit:
            self.flush()

    def flush(self) -> None:
        """Flush buffered entries to disk."""
        if not self._buffer:
            return
        try:
            self._ensure_file()
            assert self._file_handle is not None
            for entry in self._buffer:
                self._file_handle.write(json.dumps(entry, default=str) + "\n")
            self._file_handle.flush()
        except Exception:
            pass
        self._buffer.clear()

    def close(self) -> None:
        """Flush and close the log file."""
        self.flush()
        if self._file_handle is not None:
            try:
                self._file_handle.close()
            except Exception:
                pass
            self._file_handle = None

    # ── Typed log methods ──────────────────────────────────────────────

    def tool_call(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        duration_ms: int = 0,
        ok: bool = True,
        error: str = "",
        result_preview: str = "",
    ) -> None:
        """Log a tool invocation."""
        # Truncate large args for logging.
        safe_args = {}
        if args:
            for k, v in args.items():
                sv = str(v)
                safe_args[k] = sv[:200] if len(sv) > 200 else sv

        self._write({
            "type": "tool_call",
            "data": {
                "tool": tool_name,
                "args": safe_args,
                "duration_ms": duration_ms,
                "ok": ok,
                "error": error[:500] if error else "",
                "result_preview": result_preview[:200],
            },
        })

    def api_request(
        self,
        model: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_hit: bool = False,
        latency_ms: int = 0,
        error: str = "",
    ) -> None:
        """Log an API request to the LLM backend."""
        self._write({
            "type": "api_request",
            "data": {
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_hit": cache_hit,
                "latency_ms": latency_ms,
                "error": error[:500] if error else "",
            },
        })

    def event(
        self,
        event_type: str,
        **data: Any,
    ) -> None:
        """Log a general event."""
        safe_data = {}
        for k, v in data.items():
            sv = str(v)
            safe_data[k] = sv[:500] if len(sv) > 500 else sv
        self._write({
            "type": "event",
            "data": {"event": event_type, **safe_data},
        })

    def error(
        self,
        message: str,
        *,
        source: str = "",
        exc_type: str = "",
    ) -> None:
        """Log an error."""
        self._write({
            "type": "error",
            "data": {
                "message": message[:1000],
                "source": source,
                "exc_type": exc_type,
            },
        })

    def session_event(
        self,
        action: str,
        session_id: str = "",
        **extra: Any,
    ) -> None:
        """Log a session lifecycle event."""
        self._write({
            "type": "session",
            "data": {"action": action, "session_id": session_id[:16], **extra},
        })

    @property
    def total_entries(self) -> int:
        return self._total_entries

    @property
    def log_path(self) -> str:
        return str(self._log_file or _LOG_DIR / "deep.jsonl")


# ── Module singleton ───────────────────────────────────────────────────

_enabled = os.environ.get("OBSCURA_DEEP_LOG", "1").strip().lower() not in ("0", "false", "no", "off")
dlog = DeepLogger(enabled=_enabled)
