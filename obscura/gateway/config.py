"""Gateway configuration with support for all three modes."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any


class GatewayMode(Enum):
    """Operational modes for the Obscura Gateway."""
    
    AUTO = auto()      # Automatically select best available mode
    OPENCLAW = auto()  # Run as OpenClaw agent with tool delegation
    NATIVE = auto()    # Standalone with direct system access
    MCP = auto()       # MCP bridge server only
    HYBRID = auto()    # Native with OpenClaw fallback


@dataclass
class SecurityConfig:
    """Security and authentication configuration."""
    
    auth_type: str = "token"  # token | oauth | none
    token_file: Path | None = None
    rate_limit_requests_per_minute: int = 60
    rate_limit_burst: int = 10
    require_approval_for_destructive: bool = True
    require_approval_for_file_deletion: bool = True
    require_approval_for_network: bool = False
    
    def __post_init__(self):
        if self.token_file is None:
            self.token_file = Path.home() / ".obscura" / "gateway.token"


@dataclass
class OpenClawConfig:
    """OpenClaw integration configuration."""
    
    enabled: bool = True
    socket_path: Path | None = None
    gateway_url: str = "ws://127.0.0.1:18789"
    fallback_on_disconnect: bool = True
    delegate_system_tools: bool = True
    
    def __post_init__(self):
        if self.socket_path is None:
            self.socket_path = Path.home() / ".openclaw" / "gateway.sock"


@dataclass
class NativeConfig:
    """Native gateway mode configuration."""
    
    enabled: bool = True
    port: int = 18790  # Obscura's native port (separate from OpenClaw's 18789)
    host: str = "127.0.0.1"
    elevated: bool = False  # Requires sudo for full system access
    sandbox: bool = False   # If True, restricts system access
    
    # System tool configuration
    allow_shell_exec: bool = True
    allow_file_write: bool = True
    allow_process_management: bool = True
    allow_network_access: bool = True


@dataclass
class MCPConfig:
    """MCP Bridge mode configuration."""
    
    enabled: bool = True
    port: int = 18791
    host: str = "127.0.0.1"
    transport: str = "stdio"  # stdio | sse
    expose_obscura_tools: bool = True
    delegate_system_tools: bool = True


@dataclass
class AuditConfig:
    """Audit logging configuration."""
    
    enabled: bool = True
    log_dir: Path | None = None
    max_file_size: str = "100MB"
    retention_days: int = 30
    
    def __post_init__(self):
        if self.log_dir is None:
            self.log_dir = Path.home() / ".obscura" / "audit"


@dataclass
class GatewayConfig:
    """Complete gateway configuration."""
    
    # Mode selection
    mode: GatewayMode = GatewayMode.AUTO
    mode_priority: list[GatewayMode] = field(default_factory=lambda: [
        GatewayMode.OPENCLAW,
        GatewayMode.NATIVE,
        GatewayMode.MCP,
    ])
    
    # Sub-configurations
    openclaw: OpenClawConfig = field(default_factory=OpenClawConfig)
    native: NativeConfig = field(default_factory=NativeConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    
    # Feature flags
    hot_swap_modes: bool = True  # Allow mode switching without restart
    enable_websocket: bool = True
    enable_rest: bool = True
    enable_grpc: bool = False
    
    @classmethod
    def from_env(cls) -> GatewayConfig:
        """Load configuration from environment variables."""
        config = cls()
        
        # Mode selection
        mode_str = os.environ.get("OBSCURA_GATEWAY_MODE", "auto").upper()
        if hasattr(GatewayMode, mode_str):
            config.mode = GatewayMode[mode_str]
        
        # Native config
        if port := os.environ.get("OBSCURA_GATEWAY_PORT"):
            config.native.port = int(port)
        if host := os.environ.get("OBSCURA_GATEWAY_HOST"):
            config.native.host = host
        
        # Security
        if auth := os.environ.get("OBSCURA_GATEWAY_AUTH"):
            config.security.auth_type = auth
        
        # OpenClaw
        config.openclaw.enabled = os.environ.get(
            "OBSCURA_OPENCLAW_ENABLED", "true"
        ).lower() == "true"
        
        return config
    
    @classmethod
    def from_file(cls, path: Path | str) -> GatewayConfig:
        """Load configuration from YAML file."""
        import yaml
        
        path = Path(path)
        if not path.exists():
            return cls.from_env()
        
        with open(path) as f:
            data = yaml.safe_load(f)
        
        return cls._from_dict(data)
    
    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> GatewayConfig:
        """Create config from dictionary."""
        # Simplified - would need full implementation
        return cls.from_env()
    
    def to_dict(self) -> dict[str, Any]:
        """Convert config to dictionary."""
        return {
            "mode": self.mode.name,
            "native": {
                "enabled": self.native.enabled,
                "port": self.native.port,
                "host": self.native.host,
            },
            "openclaw": {
                "enabled": self.openclaw.enabled,
                "fallback_on_disconnect": self.openclaw.fallback_on_disconnect,
            },
            "mcp": {
                "enabled": self.mcp.enabled,
                "port": self.mcp.port,
            },
        }
