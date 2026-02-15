#!/usr/bin/env python3
"""Generate API keys for Obscura.

Usage:
    python scripts/generate_api_key.py [name] [roles...]
    
Examples:
    python scripts/generate_api_key.py my-app admin agent:copilot
    python scripts/generate_api_key.py readonly agent:read
    
Environment:
    Set OBSCURA_API_KEYS in your shell to use custom keys.
    Format: key1:user1:role1,role2;key2:user2:role3
"""

import secrets
import sys
import os


def generate_api_key(name: str = "app", roles: list[str] | None = None) -> tuple[str, str]:
    """Generate a new API key and its OBSCURA_API_KEYS entry."""
    if roles is None:
        roles = ["agent:read", "agent:copilot"]
    
    # Generate random key
    key = f"obscura_{secrets.token_urlsafe(32)}"
    
    # Format for env var
    roles_str = ",".join(roles)
    env_entry = f"{key}:{name}:{roles_str}"
    
    return key, env_entry


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python generate_api_key.py [name] [roles...]")
        print()
        print("Examples:")
        print('  python generate_api_key.py my-app admin agent:copilot')
        print('  python generate_api_key.py readonly agent:read')
        print()
        print("Available roles:")
        print("  admin - Full access")
        print("  agent:copilot - Use Copilot backend")
        print("  agent:claude - Use Claude backend")
        print("  agent:read - Read-only access")
        print("  sync:write - Trigger sync operations")
        print("  sessions:manage - Create/delete sessions")
        return
    
    name = sys.argv[1]
    roles = sys.argv[2:] if len(sys.argv) > 2 else ["agent:read", "agent:copilot"]
    
    key, env_entry = generate_api_key(name, roles)
    
    print("=" * 70)
    print("✅ API KEY GENERATED")
    print("=" * 70)
    print()
    print(f"Key: {key}")
    print()
    print("Usage:")
    print("-" * 70)
    print(f'  curl -H "X-API-Key: {key}" \\\\')
    print('       http://localhost:8080/api/v1/agents')
    print()
    print("Or with Python:")
    print("-" * 70)
    print(f'  headers = {{"X-API-Key": "{key}"}}')
    print('  response = requests.get("http://localhost:8080/api/v1/agents", headers=headers)')
    print()
    print("Environment Setup:")
    print("-" * 70)
    print(f'  export OBSCURA_API_KEYS="{env_entry}"')
    print()
    print("To add multiple keys, separate with semicolons:")
    print("-" * 70)
    print('  export OBSCURA_API_KEYS="key1:app1:admin;key2:app2:agent:read"')
    print()
    
    # Check if default key exists
    current_keys = os.environ.get("OBSCURA_API_KEYS", "")
    if current_keys:
        print("Current OBSCURA_API_KEYS env var is set.")
        print(f"To add this key: export OBSCURA_API_KEYS=\"{current_keys};{env_entry}\"")
    else:
        print("No custom API keys set. The default dev key is active.")
        print("To use this custom key, set the env var above.")


if __name__ == "__main__":
    main()
