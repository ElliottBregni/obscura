"""Gateway orchestrator - manages all three operational modes."""

from __future__ import annotations

import asyncio
import logging
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

from obscura.gateway.config import GatewayConfig, GatewayMode

if TYPE_CHECKING:
    from obscura.gateway.modes.base import BaseMode

logger = logging.getLogger(__name__)


class GatewayState(Enum):
    """Gateway lifecycle states."""
    
    INITIALIZING = auto()
    RUNNING = auto()
    SWITCHING_MODE = auto()
    DEGRADED = auto()
    SHUTDOWN = auto()


class GatewayOrchestrator:
    """Orchestrates the Obscura Gateway across all three modes.
    
    The orchestrator:
    1. Selects the best available mode based on config and environment
    2. Manages mode switching (hot-swap without restart)
    3. Provides unified interface regardless of active mode
    4. Handles fallback when primary mode fails
    5. Coordinates system tool access across modes
    
    Example:
        >>> config = GatewayConfig(mode=GatewayMode.AUTO)
        >>> gateway = GatewayOrchestrator(config)
        >>> await gateway.start()
        >>> # Gateway automatically selects best mode
        >>> await gateway.execute_tool("exec", "ls -la")
        >>> await gateway.stop()
    """
    
    def __init__(self, config: GatewayConfig | None = None) -> None:
        """Initialize the gateway orchestrator.
        
        Args:
            config: Gateway configuration. If None, loads from env/file.
        """
        self.config = config or GatewayConfig.from_env()
        self.state = GatewayState.INITIALIZING
        self._current_mode: GatewayMode | None = None
        self._mode_instance: BaseMode | None = None
        self._mode_lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()
        
        # Mode registry
        self._modes: dict[GatewayMode, type[BaseMode]] = {}
        self._register_modes()
    
    def _register_modes(self) -> None:
        """Register available gateway modes."""
        from obscura.gateway.modes.openclaw import OpenClawMode  # noqa: PLC0415
        from obscura.gateway.modes.native import NativeMode  # noqa: PLC0415
        from obscura.gateway.modes.mcp import MCPMode  # noqa: PLC0415
        
        self._modes[GatewayMode.OPENCLAW] = OpenClawMode
        self._modes[GatewayMode.NATIVE] = NativeMode
        self._modes[GatewayMode.MCP] = MCPMode
        self._modes[GatewayMode.HYBRID] = NativeMode  # Hybrid uses Native with fallback
    
    async def start(self) -> None:
        """Start the gateway with automatic mode selection."""
        logger.info(f"Starting Obscura Gateway (mode: {self.config.mode.name})")
        
        # Select and initialize mode
        await self._select_and_start_mode()
        
        self.state = GatewayState.RUNNING
        logger.info(f"Gateway running in {self._current_mode.name} mode")
    
    async def _select_and_start_mode(self) -> None:
        """Select and start the best available mode."""
        if self.config.mode == GatewayMode.AUTO:
            modes_to_try = self.config.mode_priority
        else:
            modes_to_try = [self.config.mode]
        
        for mode in modes_to_try:
            if await self._try_start_mode(mode):
                return
        
        # All modes failed
        raise RuntimeError("No gateway mode could be started")
    
    async def _try_start_mode(self, mode: GatewayMode) -> bool:
        """Attempt to start a specific mode.
        
        Returns:
            True if mode started successfully, False otherwise.
        """
        if mode not in self._modes:
            logger.warning(f"Mode {mode.name} not registered")
            return False
        
        mode_class = self._modes[mode]
        
        try:
            # Check if mode is available
            if not await mode_class.is_available(self.config):
                logger.debug(f"Mode {mode.name} not available")
                return False
            
            # Start the mode
            instance = mode_class(self.config)
            await instance.start()
            
            self._current_mode = mode
            self._mode_instance = instance
            
            logger.info(f"Started gateway in {mode.name} mode")
            return True
            
        except Exception as e:
            logger.warning(f"Failed to start {mode.name} mode: {e}")
            return False
    
    async def switch_mode(self, new_mode: GatewayMode) -> bool:
        """Hot-swap to a different mode without full restart.
        
        Args:
            new_mode: The mode to switch to.
            
        Returns:
            True if switch was successful.
        """
        if not self.config.hot_swap_modes:
            logger.error("Hot-swap is disabled in config")
            return False
        
        if self._current_mode == new_mode:
            return True
        
        async with self._mode_lock:
            self.state = GatewayState.SWITCHING_MODE
            
            # Store current state for transfer
            old_instance = self._mode_instance
            
            try:
                # Start new mode
                if await self._try_start_mode(new_mode):
                    # Stop old mode
                    if old_instance:
                        await old_instance.stop()
                    
                    self.state = GatewayState.RUNNING
                    return True
                else:
                    # Restore old mode
                    self._mode_instance = old_instance
                    self.state = GatewayState.DEGRADED
                    return False
                    
            except Exception as e:
                logger.error(f"Mode switch failed: {e}")
                self._mode_instance = old_instance
                self.state = GatewayState.DEGRADED
                return False
    
    async def execute_tool(
        self,
        tool_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute a tool through the current mode.
        
        This provides a unified interface regardless of which mode
        is currently active.
        
        Args:
            tool_name: Name of the tool to execute.
            *args: Positional arguments for the tool.
            **kwargs: Keyword arguments for the tool.
            
        Returns:
            Tool execution result.
        """
        if self._mode_instance is None:
            raise RuntimeError("Gateway not started")
        
        if self.state != GatewayState.RUNNING:
            raise RuntimeError(f"Gateway not running (state: {self.state.name})")
        
        return await self._mode_instance.execute_tool(
            tool_name, *args, **kwargs
        )
    
    async def get_status(self) -> dict[str, Any]:
        """Get current gateway status.
        
        Returns:
            Status dictionary with mode, state, and health info.
        """
        return {
            "state": self.state.name,
            "mode": self._current_mode.name if self._current_mode else None,
            "config": self.config.to_dict(),
            "mode_status": await self._mode_instance.get_status()
            if self._mode_instance else None,
        }
    
    async def stop(self) -> None:
        """Stop the gateway and cleanup resources."""
        logger.info("Stopping Obscura Gateway")
        
        self._shutdown_event.set()
        
        if self._mode_instance:
            await self._mode_instance.stop()
            self._mode_instance = None
        
        self._current_mode = None
        self.state = GatewayState.SHUTDOWN
        
        logger.info("Gateway stopped")
    
    async def __aenter__(self) -> GatewayOrchestrator:
        """Async context manager entry."""
        await self.start()
        return self
    
    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        await self.stop()
