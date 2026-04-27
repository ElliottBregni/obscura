"""Unit tests for the active-host registry."""

from __future__ import annotations

import json
import os
from pathlib import Path

from obscura.integrations.browser import active_hosts


def test_register_and_list(tmp_path: Path) -> None:
    reg = tmp_path / "active.json"
    active_hosts.register(
        pid=os.getpid(),
        socket=tmp_path / "x.sock",
        profile_id="abc",
        browser="chrome",
        version="0.1.0",
        path=reg,
    )
    hosts = active_hosts.list_hosts(reg)
    assert len(hosts) == 1
    assert hosts[0]["profile_id"] == "abc"
    assert hosts[0]["browser"] == "chrome"


def test_register_is_idempotent(tmp_path: Path) -> None:
    reg = tmp_path / "active.json"
    for _ in range(3):
        active_hosts.register(
            pid=os.getpid(), socket=tmp_path / "x.sock", path=reg
        )
    assert len(active_hosts.list_hosts(reg)) == 1


def test_unregister(tmp_path: Path) -> None:
    reg = tmp_path / "active.json"
    active_hosts.register(pid=os.getpid(), socket=tmp_path / "x.sock", path=reg)
    active_hosts.unregister(pid=os.getpid(), path=reg)
    assert active_hosts.list_hosts(reg) == []


def test_stale_pid_pruned(tmp_path: Path) -> None:
    """A pid that no longer exists must be filtered out by readers."""
    reg = tmp_path / "active.json"
    # 999999 is exceedingly unlikely to be a live pid — readers should drop it.
    reg.write_text(
        json.dumps(
            {
                "hosts": [
                    {
                        "pid": 999_999,
                        "socket": str(tmp_path / "stale.sock"),
                        "started_at": 1.0,
                    },
                    {
                        "pid": os.getpid(),
                        "socket": str(tmp_path / "live.sock"),
                        "started_at": 2.0,
                    },
                ]
            }
        )
    )
    hosts = active_hosts.list_hosts(reg)
    assert len(hosts) == 1
    assert int(hosts[0]["pid"]) == os.getpid()


def test_pick_filters_by_profile_and_browser(tmp_path: Path) -> None:
    reg = tmp_path / "active.json"
    active_hosts.register(
        pid=os.getpid(),
        socket=tmp_path / "x.sock",
        profile_id="alpha",
        browser="chrome",
        path=reg,
    )
    assert active_hosts.pick(profile_id="alpha", path=reg) is not None
    assert active_hosts.pick(profile_id="missing", path=reg) is None
    assert active_hosts.pick(browser="brave", path=reg) is None


def test_corrupt_registry_recovers(tmp_path: Path) -> None:
    reg = tmp_path / "active.json"
    reg.write_text("{not json")
    # list_hosts treats corrupt files as empty, doesn't raise.
    assert active_hosts.list_hosts(reg) == []
