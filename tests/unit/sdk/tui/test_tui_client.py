#!/usr/bin/env python3
"""Test TUI client connectivity."""

import asyncio
import pytest
from sdk.tui.client import TUIClient


@pytest.mark.e2e
async def test():
    """Test TUI client."""
    print("Testing TUI Client...")
    print("=" * 40)
    
    async with TUIClient() as client:
        # Health check
        print("\n1. Health check...")
        health = await client.health()
        print(f"   Status: {health['status']}")
        
        # List agents
        print("\n2. Listing agents...")
        agents = await client.list_agents()
        print(f"   Found {len(agents)} agent(s)")
        for a in agents:
            print(f"   - {a.get('name', 'Unnamed')} ({a.get('status', 'UNKNOWN')})")
        
        # Get stats
        print("\n3. Getting stats...")
        stats = await client.get_stats()
        print(f"   Active: {stats['active']}")
        print(f"   Running: {stats['running']}")
        print(f"   Waiting: {stats['waiting']}")
        print(f"   Memory: {stats['memory']}")
        
        # Spawn test agent
        print("\n4. Spawning test agent...")
        agent = await client.spawn_agent("test-tui", "claude")
        print(f"   Created: {agent['name']} ({agent['agent_id'][:8]}...)")
        
        # List again
        print("\n5. Listing agents again...")
        agents = await client.list_agents()
        print(f"   Found {len(agents)} agent(s)")
        
        # Stop agent
        print("\n6. Stopping test agent...")
        await client.stop_agent(agent['agent_id'])
        print("   Stopped.")
        
        # Memory test
        print("\n7. Testing memory...")
        await client.set_memory("tui-test", "key1", {"value": "hello"})
        value = await client.get_memory("tui-test", "key1")
        print(f"   Stored/retrieved: {value}")
        await client.delete_memory("tui-test", "key1")
        print("   Deleted.")
    
    print("\n" + "=" * 40)
    print("✓ All TUI client tests passed!")


if __name__ == "__main__":
    asyncio.run(test())
