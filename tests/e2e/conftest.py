"""
End-to-End Tests for Obscura

These tests verify complete workflows from API to response.
They require a running Obscura server.

Usage:
    # Run all tests (requires server on localhost:8080)
    pytest tests/e2e/ -v
    
    # Run only unit tests (fast)
    pytest tests/ -v -m "not e2e"
    
    # Run with custom server URL
    OBSCURA_URL=http://localhost:9000 pytest tests/e2e/ -v
"""

import os
import pytest

# Skip all e2e tests if OBSCURA_URL not set (unless --run-e2e flag)
def pytest_collection_modifyitems(config, items):
    """Skip e2e tests unless explicitly requested or OBSCURA_URL is set."""
    skip_e2e = pytest.mark.skip(reason="E2E tests skipped. Set OBSCURA_URL or use --run-e2e")
    
    if not os.environ.get("OBSCURA_URL") and not config.getoption("--run-e2e"):
        for item in items:
            if "e2e" in item.nodeid or item.get_closest_marker("e2e"):
                item.add_marker(skip_e2e)


def pytest_addoption(parser):
    """Add --run-e2e flag to pytest."""
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="Run end-to-end tests (requires server)"
    )
