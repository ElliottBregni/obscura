"""Integration test: SocketBridge with multiple concurrent clients.

The unit suite (``tests/unit/obscura/integrations/browser/test_bridge_roundtrip.py``)
already exercises multiplexing of concurrent calls over **one** connection. This
test extends that to **multiple** connections — three independent
``BrowserBridgeClient``s talking to the same ``SocketBridge``, each issuing
five overlapping calls. The goal is to flush out cross-talk bugs in request-id
routing or per-peer task tracking.

No Chrome is involved; the bridge dispatches into a stub call function that
records the (peer_id, name, args) tuple it saw so we can verify each client got
its own results back.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from obscura.integrations.browser.client import BrowserBridgeClient
from obscura.integrations.browser.server import SocketBridge

pytestmark = pytest.mark.integration


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A short tmpdir under /tmp.

    The default pytest tmp_path easily exceeds AF_UNIX's 104-byte limit on
    macOS, which silently truncates and fails to bind.
    """
    d = Path("/tmp") / f"obs-br-int-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=False)
    try:
        yield d
    finally:
        for p in d.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            d.rmdir()
        except OSError:
            pass


def _stub_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "browser_read_page",
            "description": "stub",
            "parameters": {"type": "object", "properties": {}},
            "side_effects": "none",
        },
        {
            "name": "browser_navigate",
            "description": "stub",
            "parameters": {"type": "object", "properties": {}},
            "side_effects": "mutating",
        },
    ]


@pytest.mark.asyncio
async def test_three_clients_fifteen_concurrent_calls_no_crosstalk(
    short_tmp: Path,
) -> None:
    """Three clients, five overlapping calls each — every reply lands on its
    originating client and matches the request payload.

    Replies are intentionally returned out-of-order (the dispatcher sleeps
    proportional to a per-call ``ms`` arg) so a request-id routing bug would
    cause a client to see another client's value.
    """
    sock = short_tmp / "bridge.sock"

    async def call(name: str, args: dict[str, Any]) -> Any:
        # Sleep proportional to ms so the bridge has many in-flight tasks
        # at once and replies come back interleaved.
        await asyncio.sleep(args.get("ms", 0) / 1000.0)
        return {
            "name": name,
            "ms": args.get("ms"),
            "marker": args.get("marker"),
        }

    bridge = SocketBridge(path=sock, tools_provider=_stub_specs, call=call)
    await bridge.start()
    try:
        clients = await asyncio.gather(
            BrowserBridgeClient.connect(socket_path=sock),
            BrowserBridgeClient.connect(socket_path=sock),
            BrowserBridgeClient.connect(socket_path=sock),
        )
        try:
            # Each client owns a unique marker; each issues 5 calls with
            # varying sleep times so reply order != request order.
            async def fire(client: BrowserBridgeClient, marker: str) -> list[Any]:
                # Mix tool names + sleep durations within each client.
                jobs = [
                    (
                        "browser_read_page" if i % 2 == 0 else "browser_navigate",
                        {"ms": (5 - i) * 12, "marker": marker, "i": i},
                    )
                    for i in range(5)
                ]
                return await asyncio.gather(
                    *(client.call(name, args, timeout=5.0) for name, args in jobs)
                )

            results = await asyncio.gather(
                fire(clients[0], "alpha"),
                fire(clients[1], "beta"),
                fire(clients[2], "gamma"),
            )
        finally:
            await asyncio.gather(*(c.close() for c in clients))

        # Every client should see only its own marker, in the order it
        # issued the calls.
        markers = ["alpha", "beta", "gamma"]
        for client_results, marker in zip(results, markers, strict=True):
            assert len(client_results) == 5, (
                f"client {marker} got {len(client_results)} results, expected 5"
            )
            for r in client_results:
                assert r["marker"] == marker, (
                    f"cross-talk: client {marker} saw marker={r['marker']!r} — "
                    "a request id routed to the wrong client"
                )

        # Sanity: 15 calls total, all distinct {marker, i} pairs.
        flat = [item for sub in results for item in sub]
        seen = {(r["marker"], r["ms"]) for r in flat}
        assert len(flat) == 15
        assert len(seen) == 15, f"duplicate replies detected: {flat!r}"
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_one_client_disconnect_does_not_disrupt_others(
    short_tmp: Path,
) -> None:
    """Closing one client mid-flight must not cancel work on sibling clients.

    The bridge tracks in-flight tasks per peer, so a peer disconnect should
    only cancel that peer's tasks — siblings keep dispatching.
    """
    sock = short_tmp / "bridge.sock"
    started: list[str] = []
    completed: list[str] = []

    async def call(_name: str, args: dict[str, Any]) -> Any:
        marker = str(args.get("marker") or "")
        started.append(marker)
        await asyncio.sleep(args.get("ms", 0) / 1000.0)
        completed.append(marker)
        return {"marker": marker}

    bridge = SocketBridge(path=sock, tools_provider=_stub_specs, call=call)
    await bridge.start()
    try:
        c1 = await BrowserBridgeClient.connect(socket_path=sock)
        c2 = await BrowserBridgeClient.connect(socket_path=sock)

        # Kick off a slow call on c1 (which we'll close mid-flight) and
        # several quick calls on c2.
        slow_task = asyncio.create_task(
            c1.call(
                "browser_read_page",
                {"ms": 200, "marker": "doomed"},
                timeout=5.0,
            )
        )
        fast_tasks = [
            asyncio.create_task(
                c2.call(
                    "browser_navigate",
                    {"ms": 20, "marker": f"survivor-{i}"},
                    timeout=5.0,
                )
            )
            for i in range(3)
        ]

        # Let the slow call get accepted by the bridge before we yank c1.
        await asyncio.sleep(0.05)
        await c1.close()

        # The slow call should fail (its connection is gone). The fast
        # calls on c2 should still resolve cleanly.
        slow_result = await asyncio.gather(slow_task, return_exceptions=True)
        fast_results = await asyncio.gather(*fast_tasks)

        assert isinstance(slow_result[0], Exception), (
            "expected the slow call on the closed client to fail"
        )
        for i, r in enumerate(fast_results):
            assert r["marker"] == f"survivor-{i}"

        await c2.close()
    finally:
        await bridge.stop()

    # All three survivors must have completed; doomed may or may not have
    # reached completed depending on scheduling — what matters is the
    # surviving client's calls land their values.
    for i in range(3):
        assert f"survivor-{i}" in completed
