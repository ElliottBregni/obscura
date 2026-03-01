#!/usr/bin/env python3
"""Discover available MCP servers from the registry."""

from obscura.integrations.mcp.catalog import (
    MCPRegistryAPICatalogProvider,
    MCPServersOrgCatalogProvider,
)

def main():
    print("🔍 Discovering MCP Servers...\n")
    
    # Try official registry first
    print("📡 Fetching from Official MCP Registry...")
    try:
        registry = MCPRegistryAPICatalogProvider()
        entries = registry.fetch_top(limit=20)
        
        if entries:
            print(f"✅ Found {len(entries)} servers from official registry:\n")
            for entry in entries[:10]:  # Show first 10
                print(f"  {entry.rank}. {entry.name}")
                print(f"     Slug: {entry.slug}")
                print(f"     URL:  {entry.url}")
                print()
        else:
            print("⚠️  No servers found in official registry\n")
    except Exception as e:
        print(f"❌ Official registry failed: {e}\n")
    
    # Try community catalog
    print("🌐 Fetching from MCPServers.org Community Catalog...")
    try:
        community = MCPServersOrgCatalogProvider()
        entries = community.fetch_top(limit=20)
        
        if entries:
            print(f"✅ Found {len(entries)} servers from community catalog:\n")
            for entry in entries[:10]:  # Show first 10
                print(f"  {entry.rank}. {entry.name}")
                print(f"     Slug: {entry.slug}")
                print(f"     NPM:  npx -y {entry.slug}-mcp")
                print()
        else:
            print("⚠️  No servers found in community catalog\n")
    except Exception as e:
        print(f"❌ Community catalog failed: {e}\n")

if __name__ == "__main__":
    main()
