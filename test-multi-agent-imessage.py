#!/usr/bin/env python3
"""Test multi-agent group chat via iMessage.

This creates a group chat with multiple AI agents (Molty, Obscura, etc.)
all responding in the same iMessage conversation.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from obscura.gateway.multi_agent_chat import MultiAgentChatBridge
from obscura.gateway.orchestrator import GatewayOrchestrator, GatewayMode
from obscura.gateway.config import GatewayConfig
from obscura.integrations.imessage.adapter import IMessageAdapter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    """Run multi-agent iMessage chat."""

    # Your phone number
    YOUR_NUMBER = "+12316333624"

    print("🚀 Multi-Agent iMessage Chat")
    print(f"   Your number: {YOUR_NUMBER}")
    print()

    # Start gateway
    config = GatewayConfig(mode=GatewayMode.HYBRID)
    gateway = GatewayOrchestrator(config)
    await gateway.start()

    print("✅ Gateway started")

    # Create multi-agent bridge
    multi_agent = MultiAgentChatBridge(gateway)

    # Create group chat with all agents
    session = await multi_agent.create_group_chat(
        channel_id=YOUR_NUMBER,
        platform="imessage",
        agent_names=["molty", "obscura", "code_architect", "assistant"],
    )

    print(f"✅ Created group chat with {len(session.agents)} agents:")
    for agent in session.agents:
        print(f"   {agent.emoji} {agent.name}")
    print()

    # Start iMessage adapter
    imessage = IMessageAdapter(contacts=[YOUR_NUMBER])
    await imessage.start()

    print("📱 Listening for iMessages...")
    print("   Send a message to this Mac to test!")
    print("   (Press Ctrl+C to stop)")
    print()

    try:
        while True:
            # Poll for new messages
            messages = await imessage.poll()

            for msg in messages:
                print(f"📨 Received: {msg.text[:50]}...")

                # Get responses from all relevant agents
                responses = await multi_agent.handle_message(msg)

                # Send each agent's response
                for resp in responses:
                    print(f"   {resp['response'][:60]}...")

                    # Send back via iMessage
                    success = await imessage.send(msg.sender_id, resp["response"])
                    if success:
                        print(f"   ✅ Sent by {resp['agent']}")
                    else:
                        print(f"   ❌ Failed to send {resp['agent']}")

                print()

            await asyncio.sleep(2)

    except KeyboardInterrupt:
        print("\n🛑 Stopping...")
    finally:
        await gateway.stop()
        print("✅ Stopped")


if __name__ == "__main__":
    asyncio.run(main())
