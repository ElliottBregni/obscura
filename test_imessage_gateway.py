#!/usr/bin/env python3
"""Test iMessage integration with Obscura Gateway.

This script:
1. Starts the iMessage adapter
2. Connects it to the gateway messaging bridge
3. Polls for new messages and routes them to sessions
4. Sends responses back via iMessage
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

# Add obscura to path
sys.path.insert(0, str(Path(__file__).parent))

from obscura.gateway.messaging_bridge import (
    MessagingSessionBridge,
    iMessageSessionAdapter,
)
from obscura.gateway.orchestrator import GatewayOrchestrator, GatewayMode
from obscura.gateway.config import GatewayConfig
from obscura.integrations.imessage.adapter import IMessageAdapter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    """Run iMessage gateway test."""
    
    # Your phone number for testing
    TEST_CONTACT = "+12316333624"  # Your number
    
    print("🚀 Starting iMessage Gateway Test")
    print(f"   Contact: {TEST_CONTACT}")
    print()
    
    # Start gateway
    config = GatewayConfig(mode=GatewayMode.HYBRID)
    gateway = GatewayOrchestrator(config)
    await gateway.start()
    
    print("✅ Gateway started on port 18790")
    
    # Create messaging bridge
    bridge = MessagingSessionBridge(gateway)
    imessage_adapter = iMessageSessionAdapter(bridge)
    
    # Start iMessage adapter
    imessage = IMessageAdapter(contacts=[TEST_CONTACT])
    await imessage.start()
    
    print("✅ iMessage adapter started")
    print("📱 Send an iMessage to this Mac to test...")
    print("   (Press Ctrl+C to stop)")
    print()
    
    try:
        while True:
            # Poll for new messages
            messages = await imessage.poll()
            
            for msg in messages:
                print(f"📨 Received from {msg.sender_id}: {msg.text[:50]}...")
                
                # Process through gateway
                response = await imessage_adapter.on_message(msg)
                
                print(f"🤖 Response: {response[:50]}...")
                
                # Send response back
                success = await imessage.send(msg.sender_id, response)
                if success:
                    print("✅ Response sent")
                else:
                    print("❌ Failed to send response")
                print()
            
            # Poll every 2 seconds
            await asyncio.sleep(2)
            
    except KeyboardInterrupt:
        print("\n🛑 Stopping...")
    finally:
        await gateway.stop()
        print("✅ Gateway stopped")


if __name__ == "__main__":
    asyncio.run(main())
