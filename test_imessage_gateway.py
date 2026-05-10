#!/usr/bin/env python3
"""Test iMessage integration through the GatewayNetworkBridge.

Flow:
    IMessageAdapter.poll()
        -> bridge.dispatch(PlatformMessage)
        -> ChannelRouter -> GatewayAgentRunner -> GatewayOrchestrator
        -> active gateway mode (HYBRID: OPENCLAW -> NATIVE -> MCP)
        -> response string
        -> IMessageAdapter.send()   <- called automatically by ChannelRouter
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
from obscura.integrations.imessage.adapter import IMessageAdapter

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

    # 2. Build iMessage adapter and register it with the router manually so
    #    we keep a reference for the poll loop.
    imessage = IMessageAdapter(contacts=[contact])
    await imessage.start()
    bridge.router.register("imessage", imessage)

    # 3. Start bridge (starts orchestrator)
    await bridge.start()

    # Print full status at startup
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

    received: int = 0
    dispatched: int = 0

    try:
        while True:
            messages = await imessage.poll()
            for msg in messages:
                received += 1
                preview = msg.text[:80] if msg.text else ""
                print(f"[{received}] {msg.sender_id}: {preview}")
                # Dispatch -> ChannelRouter handles agent run + reply automatically
                await bridge.dispatch(msg)
                dispatched += 1
                print(f"    dispatched through gateway ({dispatched} total)")
            await asyncio.sleep(poll_interval)
    except KeyboardInterrupt:
        print()
        print("Stopping...")
    finally:
        await bridge.stop()
        print()
        print("--- Summary ---")
        print(f"  Messages received:   {received}")
        print(f"  Messages dispatched: {dispatched}")
        print("Done")


if __name__ == "__main__":
    asyncio.run(main())
