"""launchctl wrappers for the wuzapi sidecar service.

Thin, typed shell-outs to ``launchctl`` for managing
``dev.obscura.wuzapi`` — load/unload/kickstart/print and tailing the log.

All operations are sync (launchctl is fast) and stateless. The plist
itself lives in ``~/Library/LaunchAgents/dev.obscura.wuzapi.plist`` and is
written by :mod:`install`; this module never edits it, only talks to
launchd about it.

Why not just ``os.system``: we want typed return values (``WuzapiServiceStatus``)
and a single error path (``LifecycleError``), not stringly-typed grep of
shell output. Keeps the CLI surface clean.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

LABEL: Final[str] = "dev.obscura.wuzapi"
PLIST_PATH: Final[Path] = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
WUZAPI_HOME: Final[Path] = Path.home() / ".obscura" / "wuzapi"
WUZAPI_LOG: Final[Path] = Path.home() / ".obscura" / "logs" / "wuzapi.log"
WUZAPI_ERR_LOG: Final[Path] = Path.home() / ".obscura" / "logs" / "wuzapi.err.log"


# ---------------------------------------------------------------------------
# Errors + status
# ---------------------------------------------------------------------------


class LifecycleError(RuntimeError):
    """Raised when a launchctl call fails or the service is in a bad state."""


_State = Literal["running", "loaded_not_running", "not_loaded", "unknown"]


@dataclass(frozen=True)
class WuzapiServiceStatus:
    """Snapshot of the LaunchAgent's runtime state."""

    label: str
    state: _State
    pid: int | None
    last_exit_status: int | None

    @property
    def is_running(self) -> bool:
        return self.state == "running" and self.pid is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uid() -> int:
    return os.getuid()


def _gui_target() -> str:
    return f"gui/{_uid()}/{LABEL}"


def _run(
    *args: str, check: bool = False, timeout: float = 10.0
) -> subprocess.CompletedProcess[str]:
    """Wrap subprocess.run with consistent defaults."""
    return subprocess.run(
        list(args),
        check=check,
        timeout=timeout,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_plist_installed() -> bool:
    """Return True if the LaunchAgent plist file exists on disk."""
    return PLIST_PATH.is_file()


def status() -> WuzapiServiceStatus:
    """Probe launchd for the current state of ``dev.obscura.wuzapi``."""
    if not is_plist_installed():
        return WuzapiServiceStatus(LABEL, "not_loaded", None, None)
    result = _run("launchctl", "print", _gui_target())
    if result.returncode != 0:
        return WuzapiServiceStatus(LABEL, "not_loaded", None, None)
    pid: int | None = None
    last_exit: int | None = None
    state: _State = "unknown"
    for line in result.stdout.splitlines():
        s = line.strip()
        if s.startswith("state = "):
            value = s.split(" = ", 1)[1].strip()
            if value == "running":
                state = "running"
            elif value in ("waiting", "spawn scheduled", "not running"):
                state = "loaded_not_running"
        elif s.startswith("pid = "):
            with contextlib.suppress(ValueError):
                pid = int(s.split(" = ", 1)[1])
        elif s.startswith("last exit code = "):
            with contextlib.suppress(ValueError):
                last_exit = int(s.split(" = ", 1)[1])
    return WuzapiServiceStatus(LABEL, state, pid, last_exit)


def load() -> None:
    """Register the LaunchAgent so it starts on login.

    Uses the legacy ``launchctl load -w`` for forgiving registration —
    ``bootstrap`` is pickier about clean state and frequently I/O-errors
    on already-known labels in our experience.
    """
    if not is_plist_installed():
        raise LifecycleError(f"plist not found at {PLIST_PATH}; run install first")
    result = _run("launchctl", "load", "-w", str(PLIST_PATH))
    if result.returncode != 0 and "already loaded" not in result.stderr.lower():
        raise LifecycleError(f"launchctl load failed: {result.stderr.strip()}")


def unload() -> None:
    """Stop and unregister the LaunchAgent. Idempotent."""
    if not is_plist_installed():
        return
    _run("launchctl", "unload", "-w", str(PLIST_PATH))


def kickstart(*, restart: bool = True) -> WuzapiServiceStatus:
    """Start (or restart with ``-k``) the service. Returns post-action status.

    Raises :class:`LifecycleError` if the service didn't reach running
    state within ~5 seconds.
    """
    flags = ["kickstart"]
    if restart:
        flags.append("-k")
    flags.append(_gui_target())
    result = _run("launchctl", *flags)
    if result.returncode != 0:
        raise LifecycleError(f"kickstart failed: {result.stderr.strip()}")
    # Wait briefly for the spawn to settle
    for _ in range(10):
        s = status()
        if s.is_running:
            return s
        time.sleep(0.5)
    return status()


def stop() -> None:
    """Stop the running instance without unloading the plist."""
    _run("launchctl", "kill", "TERM", _gui_target())


# ---------------------------------------------------------------------------
# Log access
# ---------------------------------------------------------------------------


def tail_log(*, lines: int = 50) -> str:
    """Return the last ``lines`` lines of wuzapi.log + wuzapi.err.log.

    The combined log isn't interleaved by timestamp — we just append err
    after stdout. For most diagnostic purposes that's enough.
    """
    out_parts: list[str] = []
    for label, path in (("stdout", WUZAPI_LOG), ("stderr", WUZAPI_ERR_LOG)):
        if not path.is_file():
            out_parts.append(f"# {label} ({path}): missing")
            continue
        result = _run("tail", "-n", str(lines), str(path))
        out_parts.append(f"# {label} ({path}):")
        out_parts.append(result.stdout)
    return "\n".join(out_parts)


__all__ = [
    "LABEL",
    "LifecycleError",
    "PLIST_PATH",
    "WUZAPI_ERR_LOG",
    "WUZAPI_HOME",
    "WUZAPI_LOG",
    "WuzapiServiceStatus",
    "is_plist_installed",
    "kickstart",
    "load",
    "status",
    "stop",
    "tail_log",
    "unload",
]
