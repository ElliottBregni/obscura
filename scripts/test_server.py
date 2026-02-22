#!/usr/bin/env python3
"""
Test Server Wrapper - Starts Obscura server with auth disabled for testing.

Usage:
    python scripts/test_server.py

Environment:
    OBSCURA_AUTH_ENABLED - Set to 'false' to disable auth (default: false for tests)
    OTEL_ENABLED - Set to 'false' to disable telemetry (default: false for tests)
    OBSCURA_PORT - Port to run on (default: 8080)
"""

import os
import sys

# Add parent dir to path so we can import sdk
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set defaults for testing BEFORE importing anything else
os.environ.setdefault("OBSCURA_AUTH_ENABLED", "false")
os.environ.setdefault("OTEL_ENABLED", "false")
os.environ.setdefault("OBSCURA_LOG_LEVEL", "WARNING")

# Now import after env vars are set
from obscura.server import create_app
from obscura.core.config import ObscuraConfig
import uvicorn


def main():
    """Start test server."""
    port = int(os.environ.get("OBSCURA_PORT", "8080"))
    host = os.environ.get("OBSCURA_HOST", "0.0.0.0")

    # Create config - it will read the env vars we just set
    config = ObscuraConfig.from_env()

    print(f"Starting Obscura Test Server...")
    print(f"  Host: {host}")
    print(f"  Port: {port}")
    print(f"  Auth: {'enabled' if config.auth_enabled else 'disabled'}")
    print(f"  OTel: {'enabled' if config.otel_enabled else 'disabled'}")

    if config.auth_enabled:
        print("\n⚠️  WARNING: Auth is enabled! Tests may fail with 401.")
        print("   Set OBSCURA_AUTH_ENABLED=false to disable auth for tests.")

    app = create_app(config)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
