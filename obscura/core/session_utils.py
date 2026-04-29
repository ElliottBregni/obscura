"""obscura.core.session_utils — Session lifecycle utilities.

Provides:
  - Auto-titling: AI-generate session titles from first message
  - Concurrent session detection: PID-based locking
  - Streaming idle timeout: abort hung API streams
  - Graceful shutdown: signal handler registration
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_SESSION_DIR = Path.home() / ".obscura" / "sessions"


# ═══════════════════════════════════════════════════════════════════════════
# Auto-titling
# ═══════════════════════════════════════════════════════════════════════════


async def _send_oneshot(backend: Any, prompt: str, *, timeout: float) -> Any:
    """Send *prompt* on a side channel that doesn't pollute conversation state.

    Backends with persistent server-side sessions (Copilot) implement
    ``send_isolated`` to spin up a temp session for the call. For
    stateless or HTTP-per-call backends (Anthropic, OpenAI, local LLM)
    plain ``send`` is already isolated, so we fall back to it.
    """
    if hasattr(backend, "send_isolated"):
        return await asyncio.wait_for(backend.send_isolated(prompt), timeout=timeout)
    return await asyncio.wait_for(backend.send(prompt), timeout=timeout)


async def generate_session_title(
    first_message: str,
    backend: Any,
    *,
    timeout: float = 15.0,
) -> str:
    """Auto-generate a 3-7 word session title from the first user message.

    Uses a fast LLM call with minimal tokens. Returns empty string on failure.

    The call goes through :func:`_send_oneshot` rather than the live
    backend session — for Copilot in particular, ``backend.send`` would
    persist the title-gen prompt into the active conversation history,
    leaving "Generate a concise 3-7 word title…" visible to the model on
    the next real turn.
    """
    if not first_message or len(first_message.strip()) < 5:
        return ""

    prompt = (
        "Generate a concise 3-7 word title for a coding session that starts with this message. "
        "Return ONLY the title, no quotes, no punctuation, no explanation.\n\n"
        f"Message: {first_message[:500]}"
    )

    try:
        if hasattr(backend, "send") or hasattr(backend, "send_isolated"):
            response = await _send_oneshot(backend, prompt, timeout=timeout)
            text = ""
            if hasattr(response, "content"):
                for block in response.content:
                    if hasattr(block, "text"):
                        text += block.text
            if not text:
                text = str(response)
            # Clean up: remove quotes, limit length.
            title = text.strip().strip("\"'").strip()
            if len(title) > 60:
                title = title[:57] + "..."
            return title
    except (TimeoutError, Exception):
        pass
    return ""


# ═══════════════════════════════════════════════════════════════════════════
# Concurrent session detection
# ═══════════════════════════════════════════════════════════════════════════


def register_session(session_id: str, **metadata: Any) -> Path:
    """Register this session's PID in the session directory.

    Returns the lock file path. Call ``unregister_session`` on exit.
    """
    _SESSION_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _SESSION_DIR / f"{session_id[:16]}.lock"
    data = {
        "pid": os.getpid(),
        "session_id": session_id,
        "started_at": time.time(),
        **{k: str(v) for k, v in metadata.items()},
    }
    lock_path.write_text(json.dumps(data), encoding="utf-8")
    return lock_path


def unregister_session(session_id: str) -> None:
    """Remove session lock file on exit."""
    lock_path = _SESSION_DIR / f"{session_id[:16]}.lock"
    lock_path.unlink(missing_ok=True)


def list_active_sessions() -> list[dict[str, Any]]:
    """List all active sessions, pruning dead PIDs."""
    if not _SESSION_DIR.is_dir():
        return []
    sessions: list[dict[str, Any]] = []
    for lock_file in _SESSION_DIR.glob("*.lock"):
        try:
            data = json.loads(lock_file.read_text(encoding="utf-8"))
            pid = data.get("pid", 0)
            # Check if PID is alive.
            try:
                os.kill(pid, 0)
                sessions.append(data)
            except (ProcessLookupError, PermissionError):
                # Dead PID — clean up stale lock.
                lock_file.unlink(missing_ok=True)
        except Exception:
            lock_file.unlink(missing_ok=True)
    return sessions


def check_concurrent_sessions(session_id: str) -> list[dict[str, Any]]:
    """Check for other running sessions in the same workspace.

    Returns list of concurrent sessions (excluding this one).
    """
    active = list_active_sessions()
    return [s for s in active if s.get("session_id", "")[:16] != session_id[:16]]


# ═══════════════════════════════════════════════════════════════════════════
# Streaming idle timeout
# ═══════════════════════════════════════════════════════════════════════════


async def stream_with_idle_timeout(
    stream: Any,
    idle_timeout: float = 30.0,
) -> Any:
    """Wrap an async iterator with idle timeout detection.

    If no chunks arrive for *idle_timeout* seconds, raises ``asyncio.TimeoutError``.

    Usage::

        async for chunk in stream_with_idle_timeout(backend.stream(prompt)):
            process(chunk)
    """
    aiter = stream.__aiter__()
    while True:
        try:
            chunk = await asyncio.wait_for(aiter.__anext__(), timeout=idle_timeout)
            yield chunk
        except StopAsyncIteration:
            return
        except TimeoutError:
            logger.warning("Stream idle timeout after %.0fs — aborting", idle_timeout)
            raise


# ═══════════════════════════════════════════════════════════════════════════
# Graceful shutdown
# ═══════════════════════════════════════════════════════════════════════════

_shutdown_handlers: list[Callable[[], None]] = []
_original_sigint: Any = None
_original_sigterm: Any = None


def register_shutdown_handler(handler: Callable[[], None]) -> None:
    """Register a function to call on SIGINT/SIGTERM."""
    _shutdown_handlers.append(handler)


def install_signal_handlers() -> None:
    """Install graceful shutdown handlers for SIGINT and SIGTERM."""
    global _original_sigint, _original_sigterm

    def _handle_signal(signum: int, frame: Any) -> None:
        logger.info("Signal %d received — running shutdown handlers", signum)
        for handler in _shutdown_handlers:
            with contextlib.suppress(Exception):
                handler()
        # Restore original handler and re-raise.
        if signum == signal.SIGINT and _original_sigint:
            signal.signal(signal.SIGINT, _original_sigint)
            os.kill(os.getpid(), signal.SIGINT)
        elif signum == signal.SIGTERM and _original_sigterm:
            signal.signal(signal.SIGTERM, _original_sigterm)
            os.kill(os.getpid(), signal.SIGTERM)

    _original_sigint = signal.getsignal(signal.SIGINT)
    _original_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
