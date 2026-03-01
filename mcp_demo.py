#!/usr/bin/env python3
"""
MCP Auto-Selection and Environment Setup Demo

This script demonstrates:
1. Keyword-based server auto-selection
2. Environment variable validation
3. Discovery from catalog
4. Config management
"""

from obscura.integrations.mcp.config_loader import (
    discover_mcp_servers,
    select_servers_for_task,
    _SERVER_KEYWORDS,
)
from obscura.integrations.mcp.catalog import MCPServersOrgCatalogProvider
import os

def demo_keyword_selection():
    """Demo 1: Keyword-based Auto-Selection"""
    print("\n" + "="*70)
    print("🎯 DEMO 1: Keyword-Based Auto-Selection")
    print("="*70)
    
    # Load configured servers
    servers = discover_mcp_servers('./config/mcp-config.json')
    
    print(f"\n📦 Loaded {len(servers)} configured servers:")
    for s in servers:
        print(f"  • {s.name}")
    
    # Test different task descriptions
    test_tasks = [
        "Create a GitHub pull request",
        "Query the postgres database",
        "Send a Slack message to the team",
        "Process a Stripe payment",
        "Test API with Postman",
        "Update Jira ticket status",
        "Random task with no keywords",
    ]
    
    print("\n🔍 Testing auto-selection with different tasks:\n")
    
    for task in test_tasks:
        selected = select_servers_for_task(servers, task)
        
        print(f"📋 Task: \"{task}\"")
        if selected:
            print(f"   ✅ Selected: {', '.join(selected)}")
            
            # Show matched keywords
            for name in selected:
                kws = _SERVER_KEYWORDS.get(name, (name,))
                matched = [kw for kw in kws if kw.lower() in task.lower()]
                if matched:
                    print(f"      └─ matched keyword: '{matched[0]}'")
        else:
            print("   ℹ️  No match → use all servers")
        print()


def demo_env_validation():
    """Demo 2: Environment Variable Validation"""
    print("\n" + "="*70)
    print("⚙️  DEMO 2: Environment Variable Validation")
    print("="*70)
    
    servers = discover_mcp_servers('./config/mcp-config.json')
    
    print("\n🔍 Checking environment variables...\n")
    
    has_missing = False
    for server in servers:
        if server.missing_env:
            has_missing = True
            print(f"⚠️  {server.name}")
            for var in server.missing_env:
                current = os.environ.get(var, "<not set>")
                print(f"   • {var}: {current}")
        else:
            print(f"✅ {server.name} - all env vars set")
    
    if has_missing:
        print("\n💡 To fix:")
        print("   Add these to your ~/.zshrc or ~/.bashrc:")
        
        all_missing = set()
        for s in servers:
            all_missing.update(s.missing_env)
        
        for var in sorted(all_missing):
            print(f'   export {var}="your_value_here"')
    else:
        print("\n✅ All environment variables are configured!")


def demo_discovery():
    """Demo 3: Discover Available Servers"""
    print("\n" + "="*70)
    print("🔍 DEMO 3: Discover Available MCP Servers")
    print("="*70)
    
    print("\n📡 Fetching from MCPServers.org catalog...\n")
    
    try:
        provider = MCPServersOrgCatalogProvider()
        entries = provider.fetch_top(limit=10)
        
        print(f"✅ Found {len(entries)} servers:\n")
        
        for entry in entries:
            print(f"{entry.rank}. {entry.name[:60]}")
            print(f"   Slug: {entry.slug}")
            print(f"   Install: npx -y {entry.slug}")
            print()
            
    except Exception as e:
        print(f"❌ Discovery failed: {e}")


def demo_keyword_map():
    """Demo 4: Show Keyword Mapping"""
    print("\n" + "="*70)
    print("🗺️  DEMO 4: Server Keyword Mapping")
    print("="*70)
    
    print("\n📚 Registered keywords for auto-selection:\n")
    
    for server, keywords in sorted(_SERVER_KEYWORDS.items()):
        kw_str = ", ".join(keywords[:4])
        if len(keywords) > 4:
            kw_str += f" ... (+{len(keywords)-4} more)"
        
        print(f"  {server:20} → {kw_str}")


def main():
    print("\n" + "="*70)
    print("🚀 Obscura MCP Auto-Selection & Management Demo")
    print("="*70)
    
    try:
        demo_keyword_selection()
        demo_env_validation()
        demo_keyword_map()
        demo_discovery()
        
        print("\n" + "="*70)
        print("✅ Demo Complete!")
        print("="*70)
        print("\n💡 Next Steps:")
        print("   1. Set missing environment variables")
        print("   2. Use /mcp commands in Obscura CLI")
        print("   3. Let agents auto-select servers based on task")
        print()
        
    except Exception as e:
        print(f"\n❌ Demo failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
