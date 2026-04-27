"""Integration test: fleet-observer wiring on the host.

When the multi-agent supervisor spins up agents, the host installs a
fleet observer (``_install_fleet_observer`` / ``_start_fleet_observer``
in ``packages/browser-extension/native-host/obscura_native_host.py``)
that translates ``AgentRuntime`` lifecycle events into ``fleet`` wire
frames. The full lifecycle round-trip (synthetic runtime → frame →
panel) requires importing the host module against a Python session and
patching internal hooks, which is more invasive than this integration
slot needs to be.

This test exercises the contract surface — the observer install/start
helpers exist and are async-callable — so the rest of the panel-side
unit tests in ``tests/browser_extension/test_host_lifecycle.py`` can
rely on them being part of the host's public-ish interface.
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _import_host():
    native_dir = (
        Path(__file__).resolve().parents[3]
        / "packages"
        / "browser-extension"
        / "native-host"
    )
    if str(native_dir) not in sys.path:
        sys.path.insert(0, str(native_dir))
    # Avoid the host opening real log files / sockets while we just inspect it.
    os.environ.setdefault("OBSCURA_BROWSER_SOCKET_DISABLE", "1")
    return importlib.import_module("obscura_native_host")


def test_fleet_observer_helpers_present_and_async() -> None:
    host = _import_host()
    install = getattr(host, "_install_fleet_observer", None)
    start = getattr(host, "_start_fleet_observer", None)
    assert callable(install), (
        "host must expose `_install_fleet_observer` (added in Phase 3.1)"
    )
    assert callable(start), (
        "host must expose `_start_fleet_observer` (added in Phase 3.1)"
    )
    # The start helper schedules a poll task and must be awaitable; install
    # is sync (it monkey-patches AgentRuntime).
    assert inspect.iscoroutinefunction(start), (
        "_start_fleet_observer must be async — it kicks off a poll task"
    )
