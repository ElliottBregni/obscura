"""obscura.core.deep_log — Deep structured logging for debugging and diagnostics.

Provides a centralized logging system that captures:
  - Every tool call (name, args summary, duration, result status)
  - Every API request (model, tokens in/out, latency)
  - Every agent event (kind, turn, metadata)
  - Session lifecycle events (start, compact, dream, end)

Every entry is a single JSON object emitted to a pluggable sink. The
default :class:`JSONLSink` writes rotated ``~/.obscura/logs/deep.jsonl``
files; :class:`StdoutSink` writes JSON lines to stdout (intended for
containerized deployments that pipe stdout to log aggregators).

Sink selection:
  ``OBSCURA_DEEP_LOG``       — ``0/false/no/off`` disables logging entirely.
  ``OBSCURA_DEEP_LOG_SINK``  — ``jsonl`` (default) | ``stdout`` | ``none``.

Usage::

    from obscura.core.deep_log import dlog

    dlog.tool_call("read_text_file", {"path": "foo.py"}, duration_ms=42, ok=True)
    dlog.api_request("claude-sonnet-4-5", input_tokens=1500, output_tokens=300)
    dlog.event("session_start", session_id="abc123")
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_LOG_DIR = Path.home() / ".obscura" / "logs"
_MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB per file
_MAX_LOG_FILES = 5  # Keep 5 rotated files


# ---------------------------------------------------------------------------
# Sink Protocol + implementations
# ---------------------------------------------------------------------------


@runtime_checkable
class DeepLogSink(Protocol):
    """Where structured log entries go.

    Implementations are responsible for serialization and durability.
    They are NOT responsible for buffering — :class:`DeepLogger` owns
    that and calls :meth:`write` once per buffered batch.
    """

    def write(self, entry: dict[str, Any]) -> None:
        """Emit a single structured entry. Should swallow I/O errors."""
        ...

    def close(self) -> None:
        """Release any resources held by the sink."""
        ...

    def description(self) -> str:
        """Human-readable identity of the sink (path, ``"stdout"``, ...)."""
        ...


class JSONLSink:
    """Append-only JSON-lines file with size-based rotation.

    Writes to ``~/.obscura/logs/deep.jsonl``; on rollover, files cascade
    ``deep.jsonl → deep.1.jsonl → … → deep.{MAX}.jsonl`` (oldest dropped).
    """

    def __init__(self, log_dir: Path | None = None) -> None:
        self._log_dir = log_dir if log_dir is not None else _LOG_DIR
        self._log_file = self._log_dir / "deep.jsonl"
        self._file_handle: Any = None

    def _ensure_file(self) -> None:
        if self._file_handle is not None:
            return
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._rotate_if_needed()
        self._file_handle = self._log_file.open("a", encoding="utf-8")

    def _rotate_if_needed(self) -> None:
        if not self._log_file.exists():
            return
        if self._log_file.stat().st_size < _MAX_LOG_SIZE:
            return
        # deep.jsonl → deep.1.jsonl → ... → deep.{MAX}.jsonl (deleted)
        for i in range(_MAX_LOG_FILES, 0, -1):
            old = self._log_dir / f"deep.{i}.jsonl"
            if i == _MAX_LOG_FILES and old.exists():
                old.unlink()
            elif old.exists():
                old.rename(self._log_dir / f"deep.{i + 1}.jsonl")
        self._log_file.rename(self._log_dir / "deep.1.jsonl")

    def write(self, entry: dict[str, Any]) -> None:
        try:
            self._ensure_file()
            assert self._file_handle is not None
            self._file_handle.write(json.dumps(entry, default=str) + "\n")
            self._file_handle.flush()
        except Exception:
            logger.debug("suppressed exception in JSONLSink.write", exc_info=True)

    def close(self) -> None:
        if self._file_handle is not None:
            with contextlib.suppress(Exception):
                self._file_handle.close()
            self._file_handle = None

    def description(self) -> str:
        return str(self._log_file)


class StdoutSink:
    """JSON-lines on stdout — for containerized deployments."""

    def write(self, entry: dict[str, Any]) -> None:
        try:
            sys.stdout.write(json.dumps(entry, default=str) + "\n")
            sys.stdout.flush()
        except Exception:
            logger.debug("suppressed exception in StdoutSink.write", exc_info=True)

    def close(self) -> None:
        # stdout is not ours to close.
        pass

    def description(self) -> str:
        return "stdout"


class NullSink:
    """Discards entries. Used when the logger is disabled."""

    def write(self, entry: dict[str, Any]) -> None:  # noqa: ARG002
        pass

    def close(self) -> None:
        pass

    def description(self) -> str:
        return "null"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_deep_log_sink(name: str | None = None) -> DeepLogSink:
    """Build a :class:`DeepLogSink` from an env-style name.

    ``None`` (or unset env var) → :class:`JSONLSink`.
    ``"jsonl"`` → :class:`JSONLSink`.
    ``"stdout"`` → :class:`StdoutSink`.
    ``"none"`` / ``"null"`` → :class:`NullSink`.
    """
    raw = name if name is not None else os.environ.get("OBSCURA_DEEP_LOG_SINK", "jsonl")
    chosen = raw.strip().lower()
    if chosen in ("", "jsonl", "file"):
        return JSONLSink()
    if chosen == "stdout":
        return StdoutSink()
    if chosen in ("none", "null", "off"):
        return NullSink()
    logger.warning(
        "unknown OBSCURA_DEEP_LOG_SINK=%r, falling back to jsonl", raw,
    )
    return JSONLSink()


# ---------------------------------------------------------------------------
# DeepLogger — owns buffering + typed methods, delegates I/O to the sink
# ---------------------------------------------------------------------------


class DeepLogger:
    """Structured JSON logger for deep debugging.

    Each log entry is a single JSON object with:
    - ``ts``: Unix timestamp
    - ``type``: Event type (tool_call, api_request, event, error)
    - ``data``: Event-specific payload
    """

    def __init__(
        self,
        enabled: bool = True,
        sink: DeepLogSink | None = None,
    ) -> None:
        self._enabled = enabled
        self._sink: DeepLogSink = sink if sink is not None else create_deep_log_sink()
        self._buffer: list[dict[str, Any]] = []
        self._buffer_limit = 50  # Flush every N entries
        self._total_entries = 0

    def _write(self, entry: dict[str, Any]) -> None:
        """Buffer a log entry."""
        if not self._enabled:
            return
        entry["ts"] = time.time()
        self._buffer.append(entry)
        self._total_entries += 1
        if len(self._buffer) >= self._buffer_limit:
            self.flush()

    def flush(self) -> None:
        """Drain the in-memory buffer to the sink."""
        if not self._buffer:
            return
        for entry in self._buffer:
            self._sink.write(entry)
        self._buffer.clear()

    def close(self) -> None:
        """Flush and release the sink."""
        self.flush()
        self._sink.close()

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

        self._write(
            {
                "type": "tool_call",
                "data": {
                    "tool": tool_name,
                    "args": safe_args,
                    "duration_ms": duration_ms,
                    "ok": ok,
                    "error": error[:500] if error else "",
                    "result_preview": result_preview[:200],
                },
            },
        )

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
        self._write(
            {
                "type": "api_request",
                "data": {
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_hit": cache_hit,
                    "latency_ms": latency_ms,
                    "error": error[:500] if error else "",
                },
            },
        )

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
        self._write(
            {
                "type": "event",
                "data": {"event": event_type, **safe_data},
            },
        )

    def error(
        self,
        message: str,
        *,
        source: str = "",
        exc_type: str = "",
    ) -> None:
        """Log an error."""
        self._write(
            {
                "type": "error",
                "data": {
                    "message": message[:1000],
                    "source": source,
                    "exc_type": exc_type,
                },
            },
        )

    def session_event(
        self,
        action: str,
        session_id: str = "",
        **extra: Any,
    ) -> None:
        """Log a session lifecycle event."""
        self._write(
            {
                "type": "session",
                "data": {"action": action, "session_id": session_id[:16], **extra},
            },
        )

    @property
    def total_entries(self) -> int:
        return self._total_entries

    @property
    def log_path(self) -> str:
        """Backwards-compatible identifier — sink description (file path,
        ``"stdout"``, etc.)."""
        return self._sink.description()


# ── Module singleton ───────────────────────────────────────────────────

_enabled = os.environ.get("OBSCURA_DEEP_LOG", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
dlog = DeepLogger(enabled=_enabled)
