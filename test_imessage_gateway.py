#!/usr/bin/env python3
"""Test iMessage (and optionally WhatsApp/Discord) integration through GatewayNetworkBridge.

Flow:
    GatewayPollDaemon
        -> adapter.poll()
        -> bridge.dispatch(PlatformMessage)
        -> ChannelRouter -> GatewayAgentRunner -> GatewayOrchestrator
        -> active gateway mode (HYBRID: OPENCLAW -> NATIVE -> MCP)
        -> response string
        -> adapter.send()   <- called automatically by ChannelRouter
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Add obscura to path
sys.path.insert(0, str(Path(__file__).parent))

from obscura.gateway.config import GatewayConfig, GatewayMode
from obscura.gateway.network_bridge import build_gateway_network_bridge
from obscura.gateway.poll_daemon import build_poll_daemon

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_MODE_MAP: dict[str, GatewayMode] = {
    "NATIVE": GatewayMode.NATIVE,
    "OPENCLAW": GatewayMode.OPENCLAW,
    "MCP": GatewayMode.MCP,
    "HYBRID": GatewayMode.HYBRID,
    "AUTO": GatewayMode.AUTO,
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test iMessage integration through GatewayNetworkBridge",
    )
    parser.add_argument(
        "--mode",
        choices=list(_MODE_MAP),
        default="HYBRID",
        help="Gateway mode (default: HYBRID)",
    )
    parser.add_argument(
        "--contact",
        default="+12316333624",
        help="iMessage contact to monitor (default: +12316333624)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="Polling interval in seconds (default: 2.0)",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    mode = _MODE_MAP[args.mode]
    contact: str = args.contact
    poll_interval: float = args.poll_interval

    print("Starting iMessage Gateway Test")
    print(f"  Contact:       {contact}")
    print(f"  Mode:          {args.mode}")
    print(f"  Poll interval: {poll_interval}s")
    print()

    # 1. Build bridge (orchestrator + router wired together, not yet started)
    bridge = await build_gateway_network_bridge(
        gateway_config=GatewayConfig(mode=mode),
    )

    # 2. Start bridge (starts orchestrator)
    await bridge.start()

    # 3. Build daemon — registers iMessage adapter with both bridge.router
    #    (so the router can send replies back) and the daemon (for polling).
    #    daemon.start() is called inside build_poll_daemon before returning.
    daemon = await build_poll_daemon(
        bridge,
        poll_interval=poll_interval,
        imessage_contacts=[contact],
    )

    # Print full gateway status at startup
    status = await bridge.get_status()
    print("Gateway status:")
    print(json.dumps(status, indent=2, default=str))
    print()

    active_mode: str = status.get("gateway", {}).get("mode", args.mode)
    print(f"Gateway running — mode: {active_mode}")
    print(f"iMessage adapter registered for {contact}")
    print()
    print("Send an iMessage to this Mac to test...")
    print("(Press Ctrl+C to stop)")
    print()

    try:
        tick = 0
        while True:
            await asyncio.sleep(10)
            tick += 1
            daemon_status = await daemon.get_status()
            for platform, info in daemon_status["platforms"].items():
                print(
                    f"[tick {tick}] {platform}: "
                    f"{info['messages_dispatched']} dispatched, "
                    f"task_alive={info['task_alive']}, "
                    f"last_error={info['last_error']}"
                )
    except KeyboardInterrupt:
        print()
        print("Stopping...")
    finally:
        await daemon.stop()
        await bridge.stop()

        daemon_status = await daemon.get_status()
        print()
        print("--- Summary ---")
        for platform, info in daemon_status["platforms"].items():
            print(f"  {platform}: {info['messages_dispatched']} messages dispatched")
        print("Done")


if __name__ == "__main__":
    asyncio.run(main())
