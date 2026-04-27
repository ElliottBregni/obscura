"""Integration test: fleet-event emission from synthetic AgentRuntime lifecycle.

When obscura's multi-agent supervisor spins up agents, the panel wants live
visibility into ``agent.starting``, ``agent.ready``, ``agent.error``, and
``agent.stopped`` lifecycle events. The host is meant to install a fleet
observer (``_install_fleet_observer`` / ``_start_fleet_observer`` in
``obscura_native_host.py``) that translates each runtime event into a
``fleet`` wire frame with ``{event, agent, status}``.

This pathway is currently unimplemented in the host. The skip below is a
placeholder for the integration scenario; the next person to wire up the
fleet observer should remove the skip and assert frame emission.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skip(
    reason=(
        "TODO: fleet observer not implemented yet. "
        "packages/browser-extension/native-host/obscura_native_host.py "
        "has no `_install_fleet_observer` or `_start_fleet_observer`, and "
        "the wire protocol's `fleet` host->ext frame is currently only "
        "consumed by the panel via /diag responses. Once the host installs "
        "an observer that listens to AgentRuntime lifecycle events and "
        "emits `{type: 'fleet', event: 'agent.ready', agent: ..., status: "
        "...}`, unskip this test and assert each lifecycle transition "
        "produces a frame."
    )
)
@pytest.mark.asyncio
async def test_fleet_observer_emits_frames_for_runtime_events() -> None:
    """Placeholder for the fleet-event emission integration test."""
    msg = "fleet observer not installed in host yet"
    raise NotImplementedError(msg)
