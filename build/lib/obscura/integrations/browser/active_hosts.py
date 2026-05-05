"""active_hosts — registry of running obscura browser-extension hosts.

A separate obscura process (terminal REPL, REST API, headless agent) needs
to discover a running native host so it can connect to its socket bridge.
We maintain a tiny JSON file at ``~/.obscura/browser/active.json`` listing
every live host.

Format
~~~~~~
::

  {
    "hosts": [
      {
        "pid": 12345,
        "socket": "/tmp/obscura-browser/elliott/12345.sock",
        "profile_id": "9a7c-...",
        "browser": "chrome",
        "version": "0.4.0",
        "started_at": 1714329600.0
      },
      ...
    ]
  }

Stale-pid pruning runs on every read and write — if a recorded pid no
longer exists on the system, its entry is dropped silently. The file is
rewritten atomically (tmp + rename) so concurrent readers always see a
valid snapshot.

Multiple hosts coexist legitimately (one per Chrome profile). Clients
typically pick the most-recent entry, or filter by ``profile_id``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, cast

log = logging.getLogger("obscura.browser.active_hosts")

HostEntry = dict[str, Any]


def default_registry_path() -> Path:
    base = Path(os.environ.get("OBSCURA_HOME") or (Path.home() / ".obscura"))
    return base / "browser" / "active.json"


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        log.debug("suppressed exception in _alive", exc_info=True)
        return False
    except PermissionError:
        # Process exists but we can't signal it — still alive from our POV.
        log.debug("suppressed exception in _alive", exc_info=True)
        return True
    except OSError:
        log.debug("suppressed exception in _alive", exc_info=True)
        return False
    return True


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        log.debug("suppressed exception in _read", exc_info=True)
        return {"hosts": []}
    except (OSError, json.JSONDecodeError):
        log.warning("registry %s is corrupt; starting fresh", path)
        return {"hosts": []}
    if not isinstance(raw, dict):
        return {"hosts": []}
    return cast("dict[str, Any]", raw)


def _hosts_of(data: dict[str, Any]) -> list[HostEntry]:
    raw = data.get("hosts")
    if not isinstance(raw, list):
        return []
    items = cast("list[Any]", raw)
    return [cast("HostEntry", item) for item in items if isinstance(item, dict)]


def _pid_of(h: HostEntry) -> int:
    pid = h.get("pid", -1)
    try:
        return int(pid)
    except (TypeError, ValueError):
        log.debug("suppressed exception in _pid_of", exc_info=True)
        return -1


def list_hosts(path: Path | None = None) -> list[HostEntry]:
    """Return live host entries. Stale entries are pruned but not persisted."""
    p = path or default_registry_path()
    return [h for h in _hosts_of(_read(p)) if _alive(_pid_of(h))]


def register(
    *,
    pid: int,
    socket: Path | str,
    profile_id: str | None = None,
    browser: str | None = None,
    version: str = "",
    path: Path | None = None,
) -> None:
    """Add or refresh this host's entry. Idempotent on (pid)."""
    p = path or default_registry_path()
    kept: list[HostEntry] = [
        h for h in _hosts_of(_read(p)) if _pid_of(h) != pid and _alive(_pid_of(h))
    ]
    kept.append(
        {
            "pid": int(pid),
            "socket": str(socket),
            "profile_id": profile_id,
            "browser": browser,
            "version": version,
            "started_at": time.time(),
        }
    )
    _atomic_write(p, {"hosts": kept})


def unregister(*, pid: int, path: Path | None = None) -> None:
    """Drop this host's entry. Safe to call multiple times."""
    p = path or default_registry_path()
    with contextlib.suppress(FileNotFoundError):
        kept = [
            h for h in _hosts_of(_read(p)) if _pid_of(h) != pid and _alive(_pid_of(h))
        ]
        _atomic_write(p, {"hosts": kept})


def pick(
    *,
    profile_id: str | None = None,
    browser: str | None = None,
    path: Path | None = None,
) -> HostEntry | None:
    """Return the most-recent live host matching the filters, or None."""
    hosts = list_hosts(path)
    if profile_id is not None:
        hosts = [h for h in hosts if h.get("profile_id") == profile_id]
    if browser is not None:
        hosts = [h for h in hosts if h.get("browser") == browser]
    if not hosts:
        return None
    return max(hosts, key=lambda h: float(h.get("started_at") or 0.0))


__all__ = [
    "HostEntry",
    "default_registry_path",
    "list_hosts",
    "pick",
    "register",
    "unregister",
]
