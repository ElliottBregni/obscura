#!/usr/bin/env python3
"""
Obscura Working Demo - Complete example of using Obscura SDK.

This script demonstrates:
1. Starting a server programmatically
2. Creating an agent
3. Running a task
4. Using memory
5. Retrieving results

Usage:
    python examples/working_demo.py
"""

import asyncio
import httpx
import json

BASE_URL = "http://localhost:8080"


async def main():
    print("🚀 Obscura Working Demo")
    print("=" * 50)
    
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        # 1. Health check
        print("\n1️⃣ Checking server health...")
        resp = await client.get("/health")
        print(f"   Status: {resp.json()['status']}")
        
        # 2. Spawn an agent
        print("\n2️⃣ Creating agent...")
        resp = await client.post("/api/v1/agents", json={
            "name": "demo-agent",
            "model": "claude",
            "system_prompt": "You are a helpful assistant.",
            "memory_namespace": "demo"
        })
        agent = resp.json()
        agent_id = agent["agent_id"]
        print(f"   Created: {agent['name']} ({agent_id[:8]}...)")
        print(f"   Status: {agent['status']}")
        
        # 3. Store some memory
        print("\n3️⃣ Storing memory...")
        await client.post("/api/v1/memory/demo/context", json={
            "value": {"topic": "python", "level": "beginner"}
        })
        print("   Stored: context")
        
        # 4. Retrieve memory
        print("\n4️⃣ Retrieving memory...")
        resp = await client.get("/api/v1/memory/demo/context")
        print(f"   Value: {resp.json()['value']}")
        
        # 5. List agents
        print("\n5️⃣ Listing agents...")
        resp = await client.get("/api/v1/agents")
        agents = resp.json()["agents"]
        print(f"   Found {len(agents)} agent(s)")
        for a in agents:
            print(f"   - {a['name']} ({a['status']})")
        
        # 6. Stop the agent
        print("\n6️⃣ Stopping agent...")
        await client.delete(f"/api/v1/agents/{agent_id}")
        print("   Agent stopped")
        
        # 7. Clean up memory
        print("\n7️⃣ Cleaning up...")
        await client.delete("/api/v1/memory/demo/context")
        print("   Memory deleted")
    
    print("\n✅ Demo complete!")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
