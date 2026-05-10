"""Obscura Gateway - Triple-mode agent gateway with full machine access.

Provides three operational modes:
1. OpenClaw Agent Mode - Embedded in OpenClaw with system tool delegation
2. Native Gateway Mode - Standalone port 18789 with direct system access
3. MCP Bridge Mode - Model Context Protocol server for tool interoperability

All modes share a unified runtime with hot-swappable configuration.
"""

from __future__ import annotations

from obscura.gateway.orchestrator import GatewayMode, GatewayOrchestrator
from obscura.gateway.config import GatewayConfig
from obscura.gateway.network_bridge import (
    GatewayAgentRunner,
    GatewayNetworkBridge,
    build_gateway_network_bridge,
)

__all__ = [
    "GatewayMode",
    "GatewayOrchestrator",
    "GatewayConfig",
    "GatewayAgentRunner",
    "GatewayNetworkBridge",
    "build_gateway_network_bridge",
]
